"""Truy vấn doanh nghiệp cho giao diện: danh sách có lọc, trang chi tiết."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, select, union
from sqlalchemy.ext.asyncio import AsyncSession

from jobcrawler.common.pagination import Page, capped_count, paginate
from jobcrawler.common.schemas import JobStatus
from jobcrawler.common.text import normalize_company_name
from jobcrawler.database.models import (
    Company,
    CompanyAlias,
    CompanyRun,
    FailedPage,
    Job,
    JobPosting,
    SourceProfile,
)
from jobcrawler.modules.jobs.queries import JobItem, job_item


@dataclass(slots=True)
class CompanyFilters:
    q: str = ""
    kyc: str = ""  # verified | ambiguous | not_found | none
    run: str = ""  # trạng thái lượt mới nhất | never
    jobs: str = ""  # with | without
    page: int = 1

    def params(self) -> dict[str, Any]:
        return {"q": self.q, "kyc": self.kyc, "run": self.run, "jobs": self.jobs}


async def list_companies(session: AsyncSession, f: CompanyFilters) -> Page[Company]:
    """Lọc trên các cột ghi sẵn của `companies` (lượt mới nhất, số job đang mở) — không truy vấn
    con theo từng dòng; tìm kiếm qua `search_text` / `company_aliases.alias_norm` (GIN trigram)."""
    stmt = select(Company)
    if q := f.q.strip():
        # Hai nhánh, mỗi nhánh một index trigram, hợp bằng UNION (OR với EXISTS sẽ quét cả bảng).
        matches = union(
            select(Company.id).where(Company.search_text.ilike(f"%{q}%")),
            select(CompanyAlias.company_id).where(
                CompanyAlias.alias_norm.ilike(f"%{normalize_company_name(q)}%")
            ),
        )
        stmt = stmt.where(Company.id.in_(matches))
    if f.kyc == "none":
        stmt = stmt.where(Company.kyc_status.is_(None))
    elif f.kyc:
        stmt = stmt.where(Company.kyc_status == f.kyc)
    if f.run == "never":
        stmt = stmt.where(Company.last_run_status.is_(None))
    elif f.run:
        stmt = stmt.where(Company.last_run_status == f.run)
    if f.jobs == "with":
        stmt = stmt.where(Company.active_jobs > 0)
    elif f.jobs == "without":
        stmt = stmt.where(Company.active_jobs == 0)
    return await paginate(session, stmt, f.page, order_by=[Company.id.desc()])


@dataclass(slots=True)
class CompanyDetail:
    company: Company
    aliases: list[CompanyAlias]
    profiles: list[SourceProfile]
    inactive_jobs: int
    runs: int  # số lượt chạy (đếm có giới hạn)
    rejected: int  # số tin bị loại (đếm có giới hạn)
    failed_pages: list[FailedPage]


async def company_names(session: AsyncSession, ids: set[str]) -> dict[str, str]:
    """Tên hiển thị theo id doanh nghiệp (chuỗi); id không hợp lệ bị bỏ qua."""
    valid: list[UUID] = []
    for value in ids:
        try:
            valid.append(UUID(value))
        except ValueError:
            continue
    if not valid:
        return {}
    rows = await session.execute(
        select(
            Company.id, func.coalesce(Company.legal_name, Company.brand_name, Company.input_name)
        ).where(Company.id.in_(valid))
    )
    return {str(cid): name for cid, name in rows}


async def company_detail(session: AsyncSession, company_id: UUID) -> CompanyDetail | None:
    """Thông tin doanh nghiệp + số đếm cho các tab; danh sách việc làm / lượt chạy / tin bị loại
    lấy theo trang (`company_jobs`, `company_runs`, `company_rejected`)."""
    company = await session.get(Company, company_id)
    if company is None:
        return None
    aliases = list(
        await session.scalars(
            select(CompanyAlias)
            .where(CompanyAlias.company_id == company_id)
            .order_by(CompanyAlias.id)
            .limit(200)
        )
    )
    profiles = list(
        await session.scalars(
            select(SourceProfile)
            .where(SourceProfile.company_id == company_id)
            .order_by(SourceProfile.source)
            .limit(100)
        )
    )
    inactive, _ = await capped_count(
        session,
        select(Job.id).where(Job.company_id == company_id, Job.status != JobStatus.ACTIVE.value),
    )
    runs, _ = await capped_count(
        session, select(CompanyRun.run_id).where(CompanyRun.company_id == company_id)
    )
    rejected, _ = await capped_count(session, _rejected_query(company_id))
    recent = select(CompanyRun.run_id).where(CompanyRun.company_id == company_id)
    failed = list(
        await session.scalars(
            select(FailedPage)
            .where(FailedPage.run_id.in_(recent.order_by(CompanyRun.started_at.desc()).limit(30)))
            .order_by(FailedPage.created_at.desc())
            .limit(50)
        )
    )
    return CompanyDetail(
        company=company,
        aliases=aliases,
        profiles=profiles,
        inactive_jobs=inactive,
        runs=runs,
        rejected=rejected,
        failed_pages=failed,
    )


async def company_jobs(
    session: AsyncSession, company_id: UUID, *, active: bool = True, page: int | None = 1
) -> Page[JobItem]:
    """Việc làm của một doanh nghiệp (index company_id, status, first_seen_at)."""
    status = (
        Job.status == JobStatus.ACTIVE.value if active else Job.status != JobStatus.ACTIVE.value
    )
    now = datetime.now(UTC)
    return await paginate(
        session,
        select(Job).where(Job.company_id == company_id, status),
        page,
        order_by=[Job.first_seen_at.desc(), Job.id.desc()],
        convert=lambda row: job_item(row, now=now),
    )


async def company_runs(
    session: AsyncSession, company_id: UUID, page: int | None = 1
) -> Page[CompanyRun]:
    return await paginate(
        session,
        select(CompanyRun).where(CompanyRun.company_id == company_id),
        page,
        order_by=[CompanyRun.started_at.desc()],
    )


def _rejected_query(company_id: UUID) -> Select[JobPosting]:
    return select(JobPosting).where(
        JobPosting.company_id == company_id, JobPosting.accepted.is_(False)
    )


async def company_rejected(
    session: AsyncSession, company_id: UUID, page: int | None = 1
) -> Page[JobPosting]:
    """Tin bị loại (index ix_job_postings_company_rejected)."""
    return await paginate(
        session,
        _rejected_query(company_id),
        page,
        order_by=[JobPosting.verified_at.desc(), JobPosting.id.desc()],
    )
