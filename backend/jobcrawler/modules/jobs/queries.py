"""Truy vấn việc làm cho giao diện: danh sách có lọc + phân trang, chi tiết, kiểm tay."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from jobcrawler.common.pagination import Page, paginate
from jobcrawler.common.schemas import EmploymentType, JobLevel, JobOut, JobStatus, WorkMode
from jobcrawler.database.models import Company, Job, JobPosting, JobReview
from jobcrawler.pipeline.provinces import PROVINCES
from jobcrawler.pipeline.store import job_to_out

PROVINCE_NAMES = sorted(PROVINCES)


class JobItem(JobOut):
    """`JobOut` kèm id dòng `jobs` và kết quả kiểm tay (để giao diện/API dẫn link)."""

    id: UUID
    reviewed: bool | None = None  # None: chưa kiểm; True/False: đúng/sai


def job_item(row: Job, *, now: datetime, reviewed: bool | None = None) -> JobItem:
    out = job_to_out(row, now=now)
    return JobItem.model_validate({**out.model_dump(), "id": row.id, "reviewed": reviewed})


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
    company_id: UUID | None = None
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
    """Một trang việc làm. Mỗi kiểu sắp xếp có index (status, khoá sắp xếp, id); tìm kiếm tiêu đề /
    tên doanh nghiệp dùng GIN trigram; tổng đếm tối đa COUNT_CAP."""
    review = select(JobReview.correct).where(JobReview.job_id == Job.id).scalar_subquery()
    now = datetime.now(UTC)
    return await paginate(
        session,
        job_query(f).add_columns(review),
        f.page,
        order_by=SORTS.get(f.sort, SORTS["new"]),
        scalars=False,
        convert=lambda r: job_item(r[0], now=now, reviewed=r[1]),
    )


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


async def job_detail(session: AsyncSession, job_id: UUID) -> JobDetail | None:
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


def enum_values(kind: type[JobLevel] | type[EmploymentType] | type[WorkMode]) -> list[str]:
    return [v.value for v in kind if v.value != "unknown"]


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
