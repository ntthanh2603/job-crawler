"""Truy vấn cho giao diện web: danh sách có lọc + phân trang, trang chi tiết."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from jobcrawler.db.models import (
    BoardIndexRun,
    Company,
    CompanyAlias,
    CompanyRun,
    FailedPage,
    Job,
    JobPosting,
    JobReview,
    Log,
    SourceFetch,
    SourceProfile,
)
from jobcrawler.pipeline.provinces import PROVINCES
from jobcrawler.pipeline.store import job_to_out
from jobcrawler.schemas import EmploymentType, JobLevel, JobOut, JobStatus, RunStatus, WorkMode

PAGE_SIZE = 50
PROVINCE_NAMES = sorted(PROVINCES)


class JobItem(JobOut):
    """`JobOut` kèm id dòng `jobs` và kết quả kiểm tay (để giao diện/API dẫn link)."""

    id: int
    reviewed: bool | None = None  # None: chưa kiểm; True/False: đúng/sai


def job_item(row: Job, *, now: datetime, reviewed: bool | None = None) -> JobItem:
    out = job_to_out(row, now=now)
    return JobItem.model_validate({**out.model_dump(), "id": row.id, "reviewed": reviewed})


@dataclass(slots=True)
class Page[T]:
    items: list[T]
    total: int
    page: int
    size: int = PAGE_SIZE

    @property
    def pages(self) -> int:
        return max(1, math.ceil(self.total / self.size))

    @property
    def first(self) -> int:
        return (self.page - 1) * self.size + 1 if self.total else 0

    @property
    def last(self) -> int:
        return min(self.total, self.page * self.size)


def _page(page: int | None) -> int:
    return max(1, page or 1)


async def _count(session: AsyncSession, stmt: Any) -> int:
    return int(await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)


# --- doanh nghiệp ------------------------------------------------------------------------------


@dataclass(slots=True)
class CompanyRow:
    company: Company
    last_status: str | None
    last_started: datetime | None
    last_duration_ms: int | None
    last_incomplete: list[str]
    active_jobs: int


@dataclass(slots=True)
class CompanyFilters:
    q: str = ""
    kyc: str = ""  # verified | ambiguous | not_found | none
    run: str = ""  # trạng thái lượt mới nhất | never
    jobs: str = ""  # with | without
    page: int = 1

    def params(self) -> dict[str, Any]:
        return {"q": self.q, "kyc": self.kyc, "run": self.run, "jobs": self.jobs}


async def list_companies(session: AsyncSession, f: CompanyFilters) -> Page[CompanyRow]:
    last = (
        select(
            CompanyRun.status,
            CompanyRun.started_at,
            CompanyRun.duration_ms,
            CompanyRun.incomplete_sources,
        )
        .where(CompanyRun.company_id == Company.id)
        .order_by(CompanyRun.started_at.desc())
        .limit(1)
        .lateral("last_run")
    )
    active = (
        select(func.count())
        .where(Job.company_id == Company.id, Job.status == JobStatus.ACTIVE.value)
        .correlate(Company)
        .scalar_subquery()
    )
    stmt = select(
        Company,
        last.c.status,
        last.c.started_at,
        last.c.duration_ms,
        last.c.incomplete_sources,
        active.label("active_jobs"),
    ).outerjoin(last, true())
    if f.q:
        pattern = f"%{f.q.strip()}%"
        alias_match = (
            select(CompanyAlias.id)
            .where(CompanyAlias.company_id == Company.id, CompanyAlias.alias.ilike(pattern))
            .exists()
        )
        stmt = stmt.where(
            or_(
                Company.input_name.ilike(pattern),
                Company.legal_name.ilike(pattern),
                Company.brand_name.ilike(pattern),
                Company.tax_code.ilike(pattern),
                Company.primary_domain.ilike(pattern),
                alias_match,
            )
        )
    if f.kyc == "none":
        stmt = stmt.where(Company.kyc_status.is_(None))
    elif f.kyc:
        stmt = stmt.where(Company.kyc_status == f.kyc)
    if f.run == "never":
        stmt = stmt.where(last.c.status.is_(None))
    elif f.run:
        stmt = stmt.where(last.c.status == f.run)
    if f.jobs == "with":
        stmt = stmt.where(active > 0)
    elif f.jobs == "without":
        stmt = stmt.where(active == 0)
    total = await _count(session, stmt)
    page = _page(f.page)
    rows = await session.execute(
        stmt.order_by(Company.id.desc()).limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE)
    )
    items = [
        CompanyRow(
            company=c,
            last_status=status,
            last_started=started,
            last_duration_ms=dur,
            last_incomplete=list(incomplete or []),
            active_jobs=int(n or 0),
        )
        for c, status, started, dur, incomplete, n in rows
    ]
    return Page(items=items, total=total, page=page)


@dataclass(slots=True)
class CompanyDetail:
    company: Company
    aliases: list[CompanyAlias]
    profiles: list[SourceProfile]
    runs: list[CompanyRun]
    jobs: list[JobItem]
    inactive_jobs: int
    rejected: list[JobPosting]
    failed_pages: list[FailedPage]


async def company_detail(session: AsyncSession, company_id: int) -> CompanyDetail | None:
    company = await session.get(Company, company_id)
    if company is None:
        return None
    now = datetime.now(UTC)
    aliases = list(
        await session.scalars(
            select(CompanyAlias)
            .where(CompanyAlias.company_id == company_id)
            .order_by(CompanyAlias.id)
        )
    )
    profiles = list(
        await session.scalars(
            select(SourceProfile)
            .where(SourceProfile.company_id == company_id)
            .order_by(SourceProfile.source)
        )
    )
    runs = list(
        await session.scalars(
            select(CompanyRun)
            .where(CompanyRun.company_id == company_id)
            .order_by(CompanyRun.started_at.desc())
            .limit(30)
        )
    )
    rows = list(
        await session.scalars(
            select(Job)
            .where(Job.company_id == company_id, Job.status == JobStatus.ACTIVE.value)
            .order_by(Job.first_seen_at.desc())
            .limit(300)
        )
    )
    inactive = await session.scalar(
        select(func.count()).where(
            Job.company_id == company_id, Job.status != JobStatus.ACTIVE.value
        )
    )
    rejected = list(
        await session.scalars(
            select(JobPosting)
            .where(JobPosting.company_id == company_id, JobPosting.accepted.is_(False))
            .order_by(JobPosting.verified_at.desc().nulls_last())
            .limit(100)
        )
    )
    run_ids = [r.run_id for r in runs]
    failed = (
        list(
            await session.scalars(
                select(FailedPage)
                .where(FailedPage.run_id.in_(run_ids))
                .order_by(FailedPage.created_at.desc())
                .limit(50)
            )
        )
        if run_ids
        else []
    )
    return CompanyDetail(
        company=company,
        aliases=aliases,
        profiles=profiles,
        runs=runs,
        jobs=[job_item(r, now=now) for r in rows],
        inactive_jobs=int(inactive or 0),
        rejected=rejected,
        failed_pages=failed,
    )


@dataclass(slots=True)
class RunDetail:
    run: CompanyRun
    company: Company | None
    fetches: list[SourceFetch]
    logs: list[Log]
    failed_pages: list[FailedPage]
    by_source: list[tuple[str, int, dict[str, int]]] = field(default_factory=list)


async def run_detail(session: AsyncSession, run_id: uuid.UUID) -> RunDetail | None:
    run = await session.get(CompanyRun, run_id)
    if run is None:
        return None
    fetches = list(
        await session.scalars(
            select(SourceFetch).where(SourceFetch.run_id == run_id).order_by(SourceFetch.id)
        )
    )
    logs = list(
        await session.scalars(select(Log).where(Log.run_id == run_id).order_by(Log.ts).limit(1000))
    )
    failed = list(await session.scalars(select(FailedPage).where(FailedPage.run_id == run_id)))
    grouped: dict[str, dict[str, int]] = {}
    for fetch in fetches:
        outcomes = grouped.setdefault(fetch.source, {})
        outcomes[fetch.outcome] = outcomes.get(fetch.outcome, 0) + 1
    return RunDetail(
        run=run,
        company=await session.get(Company, run.company_id),
        fetches=fetches,
        logs=logs,
        failed_pages=failed,
        by_source=[(s, sum(o.values()), o) for s, o in grouped.items()],
    )


# --- job ------------------------------------------------------------------------------------------

SORTS = {
    "new": (Job.first_seen_at.desc(), Job.id.desc()),
    "posted": (Job.posted_at.desc().nulls_last(), Job.id.desc()),
    "salary": (func.coalesce(Job.salary_max, Job.salary_min).desc().nulls_last(), Job.id.desc()),
    "expires": (Job.expires_at.asc().nulls_last(), Job.id.desc()),
}


@dataclass(slots=True)
class JobFilters:
    q: str = ""
    province: str = ""
    level: str = ""
    employment_type: str = ""
    work_mode: str = ""
    source: str = ""
    status: str = JobStatus.ACTIVE.value  # "" = mọi trạng thái
    salary_from: float | None = None  # triệu VND/tháng
    company_id: int | None = None
    reviewed: str = ""  # yes | no | correct | wrong
    sort: str = "new"
    page: int = 1

    def params(self) -> dict[str, Any]:
        return {
            "q": self.q,
            "province": self.province,
            "level": self.level,
            "employment_type": self.employment_type,
            "work_mode": self.work_mode,
            "source": self.source,
            "status": self.status,
            "salary_from": self.salary_from,
            "company_id": self.company_id,
            "reviewed": self.reviewed,
            "sort": self.sort,
        }


def job_query(f: JobFilters) -> Select[Job]:
    stmt = select(Job)
    if f.q:
        pattern = f"%{f.q.strip()}%"
        stmt = stmt.where(or_(Job.title.ilike(pattern), Job.company_name.ilike(pattern)))
    if f.province:
        stmt = stmt.where(Job.locations.contains([{"province": f.province}]))
    if f.level:
        stmt = stmt.where(Job.level == f.level)
    if f.employment_type:
        stmt = stmt.where(Job.employment_type == f.employment_type)
    if f.work_mode:
        stmt = stmt.where(Job.work_mode == f.work_mode)
    if f.source:
        stmt = stmt.where(func.split_part(Job.canonical_source, ":", 1) == f.source)
    if f.status:
        stmt = stmt.where(Job.status == f.status)
    if f.salary_from:
        stmt = stmt.where(
            Job.salary_currency == "VND",
            func.coalesce(Job.salary_max, Job.salary_min) >= f.salary_from * 1_000_000,
        )
    if f.company_id:
        stmt = stmt.where(Job.company_id == f.company_id)
    if f.reviewed:
        review = select(JobReview.correct).where(JobReview.job_id == Job.id)
        if f.reviewed == "no":
            stmt = stmt.where(~review.exists())
        elif f.reviewed == "yes":
            stmt = stmt.where(review.exists())
        else:
            stmt = stmt.where(review.where(JobReview.correct.is_(f.reviewed == "correct")).exists())
    return stmt


async def list_jobs(session: AsyncSession, f: JobFilters) -> Page[JobItem]:
    stmt = job_query(f)
    total = await _count(session, stmt)
    page = _page(f.page)
    review = select(JobReview.correct).where(JobReview.job_id == Job.id).scalar_subquery()
    rows = await session.execute(
        stmt.add_columns(review)
        .order_by(*SORTS.get(f.sort, SORTS["new"]))
        .limit(PAGE_SIZE)
        .offset((page - 1) * PAGE_SIZE)
    )
    now = datetime.now(UTC)
    items = [job_item(row, now=now, reviewed=correct) for row, correct in rows]
    return Page(items=items, total=total, page=page)


async def job_sources(session: AsyncSession) -> list[str]:
    kind = func.split_part(Job.canonical_source, ":", 1)
    return list(await session.scalars(select(kind).group_by(kind).order_by(kind)))


@dataclass(slots=True)
class JobDetail:
    job: JobItem
    status: str
    postings: list[JobPosting]
    review: JobReview | None
    company: Company | None


async def job_detail(session: AsyncSession, job_id: int) -> JobDetail | None:
    row = await session.get(Job, job_id)
    if row is None:
        return None
    postings = list(
        await session.scalars(
            select(JobPosting).where(JobPosting.job_id == job_id).order_by(JobPosting.id)
        )
    )
    review = await session.scalar(select(JobReview).where(JobReview.job_id == job_id))
    return JobDetail(
        job=job_item(
            row, now=datetime.now(UTC), reviewed=review.correct if review is not None else None
        ),
        status=row.status,
        postings=postings,
        review=review,
        company=await session.get(Company, row.company_id),
    )


# --- tổng quan ------------------------------------------------------------------------------------


@dataclass(slots=True)
class HourBucket:
    hour: datetime
    counts: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.counts.values())


async def runs_per_hour(session: AsyncSession, *, hours: int = 24) -> list[HourBucket]:
    """Số lượt chạy theo giờ (theo trạng thái) trong `hours` giờ qua, đủ cả giờ không có lượt."""
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(hours=hours - 1)
    hour = func.date_trunc("hour", CompanyRun.started_at)
    rows = await session.execute(
        select(hour, CompanyRun.status, func.count())
        .where(CompanyRun.started_at >= start)
        .group_by(hour, CompanyRun.status)
    )
    buckets = {
        start + timedelta(hours=i): HourBucket(start + timedelta(hours=i), {}) for i in range(hours)
    }
    for h, status, n in rows:
        key = h.astimezone(UTC).replace(tzinfo=UTC)
        if key in buckets:
            buckets[key].counts[status] = n
    return list(buckets.values())


async def recent_board_runs(session: AsyncSession, limit: int = 40) -> list[BoardIndexRun]:
    return list(
        await session.scalars(
            select(BoardIndexRun).order_by(BoardIndexRun.started_at.desc()).limit(limit)
        )
    )


async def recent_runs(session: AsyncSession, limit: int = 15) -> list[tuple[CompanyRun, str]]:
    rows = await session.execute(
        select(CompanyRun, Company.input_name)
        .join(Company, Company.id == CompanyRun.company_id)
        .order_by(CompanyRun.started_at.desc())
        .limit(limit)
    )
    return [(run, name) for run, name in rows]


async def failed_page(session: AsyncSession, page_id: int) -> FailedPage | None:
    return await session.get(FailedPage, page_id)


def enum_values(kind: type[JobLevel] | type[EmploymentType] | type[WorkMode]) -> list[str]:
    return [v.value for v in kind if v.value != "unknown"]


RUN_STATUSES = [s.value for s in RunStatus]


async def recent_reviews(
    session: AsyncSession, limit: int = 30
) -> list[tuple[JobReview, str, str]]:
    rows = await session.execute(
        select(JobReview, Job.title, Job.company_name)
        .join(Job, Job.id == JobReview.job_id)
        .order_by(JobReview.reviewed_at.desc())
        .limit(limit)
    )
    return [(review, title, company) for review, title, company in rows]
