"""Truy vấn cho giao diện web: danh sách có lọc + phân trang, trang chi tiết."""

from __future__ import annotations

import math
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, func, or_, select, union
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
from jobcrawler.text import normalize_company_name

PAGE_SIZE = 50
# Đếm tối đa chừng này dòng (COUNT trên bảng lớn có lọc rất tốn): quá thì hiện "10.000+" và chỉ
# cho đi tới trang cuối trong giới hạn — muốn xem tiếp thì lọc hẹp lại.
COUNT_CAP = 10_000
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
    exact: bool = True  # False: có hơn `total` (= COUNT_CAP) dòng

    @property
    def pages(self) -> int:
        return max(1, math.ceil(self.total / self.size))

    @property
    def first(self) -> int:
        return (self.page - 1) * self.size + 1 if self.total else 0

    @property
    def last(self) -> int:
        return min(self.total, self.page * self.size)


async def _count(session: AsyncSession, stmt: Any, cap: int = COUNT_CAP) -> tuple[int, bool]:
    """Số dòng của `stmt`, đếm tối đa `cap` (+1 để biết có vượt không): (số, có chính xác không)."""
    limited = stmt.order_by(None).limit(cap + 1).subquery()
    n = int(await session.scalar(select(func.count()).select_from(limited)) or 0)
    return min(n, cap), n <= cap


async def paginate[T](
    session: AsyncSession,
    stmt: Any,
    page: int | None,
    *,
    order_by: Sequence[Any],
    size: int = PAGE_SIZE,
    scalars: bool = True,
    convert: Callable[[Any], T] | None = None,
) -> Page[T]:
    """Một trang của `stmt` (OFFSET trên index của `order_by`), tổng đếm có giới hạn."""
    total, exact = await _count(session, stmt)
    pages = max(1, math.ceil(total / size))
    current = min(max(1, page or 1), pages)
    query = stmt.order_by(*order_by).limit(size).offset((current - 1) * size)
    rows: list[Any] = list(await (session.scalars(query) if scalars else session.execute(query)))
    items = [convert(r) for r in rows] if convert else rows
    return Page(items=items, total=total, page=current, size=size, exact=exact)


# --- doanh nghiệp ------------------------------------------------------------------------------


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


async def company_detail(session: AsyncSession, company_id: int) -> CompanyDetail | None:
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
    inactive, _ = await _count(
        session,
        select(Job.id).where(Job.company_id == company_id, Job.status != JobStatus.ACTIVE.value),
    )
    runs, _ = await _count(
        session, select(CompanyRun.run_id).where(CompanyRun.company_id == company_id)
    )
    rejected, _ = await _count(session, _rejected_query(company_id))
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
    session: AsyncSession, company_id: int, *, active: bool = True, page: int | None = 1
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
    session: AsyncSession, company_id: int, page: int | None = 1
) -> Page[CompanyRun]:
    return await paginate(
        session,
        select(CompanyRun).where(CompanyRun.company_id == company_id),
        page,
        order_by=[CompanyRun.started_at.desc()],
    )


def _rejected_query(company_id: int) -> Select[JobPosting]:
    return select(JobPosting).where(
        JobPosting.company_id == company_id, JobPosting.accepted.is_(False)
    )


async def company_rejected(
    session: AsyncSession, company_id: int, page: int | None = 1
) -> Page[JobPosting]:
    """Tin bị loại (index ix_job_postings_company_rejected)."""
    return await paginate(
        session,
        _rejected_query(company_id),
        page,
        order_by=[JobPosting.verified_at.desc(), JobPosting.id.desc()],
    )


@dataclass(slots=True)
class RunDetail:
    run: CompanyRun
    company: Company | None
    fetches: int
    logs: int
    failed_pages: list[FailedPage]
    by_source: list[tuple[str, int, dict[str, int]]] = field(default_factory=list)


async def run_detail(session: AsyncSession, run_id: uuid.UUID) -> RunDetail | None:
    """Tóm tắt một lượt chạy; request và log lấy theo trang (`run_fetches`, `run_logs`)."""
    run = await session.get(CompanyRun, run_id)
    if run is None:
        return None
    grouped: dict[str, dict[str, int]] = {}
    rows = await session.execute(
        select(SourceFetch.source, SourceFetch.outcome, func.count())
        .where(SourceFetch.run_id == run_id)
        .group_by(SourceFetch.source, SourceFetch.outcome)
    )
    for source, outcome, n in rows:
        grouped.setdefault(source, {})[outcome] = n
    logs, _ = await _count(session, select(Log.id).where(Log.run_id == run_id))
    failed = list(
        await session.scalars(select(FailedPage).where(FailedPage.run_id == run_id).limit(100))
    )
    return RunDetail(
        run=run,
        company=await session.get(Company, run.company_id),
        fetches=sum(sum(o.values()) for o in grouped.values()),
        logs=logs,
        failed_pages=failed,
        by_source=sorted(
            ((s, sum(o.values()), o) for s, o in grouped.items()), key=lambda x: -x[1]
        ),
    )


async def run_fetches(
    session: AsyncSession, run_id: uuid.UUID, page: int | None = 1
) -> Page[SourceFetch]:
    return await paginate(
        session,
        select(SourceFetch).where(SourceFetch.run_id == run_id),
        page,
        order_by=[SourceFetch.id],
    )


async def run_logs(session: AsyncSession, run_id: uuid.UUID, page: int | None = 1) -> Page[Log]:
    return await paginate(
        session,
        select(Log).where(Log.run_id == run_id),
        page,
        order_by=[Log.ts, Log.id],
        size=100,
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


async def board_runs(session: AsyncSession, page: int | None = 1) -> Page[BoardIndexRun]:
    return await paginate(
        session, select(BoardIndexRun), page, order_by=[BoardIndexRun.started_at.desc()], size=20
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
