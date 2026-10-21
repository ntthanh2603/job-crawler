"""Truy vấn việc làm cho giao diện: danh sách có lọc + phân trang, chi tiết, kiểm tay.

Thao tác việc làm cho API: export theo bộ lọc, kiểm tra tồn tại."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import Float, Select, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from jobcrawler.common.pagination import GetManyQuery, Page, SortOrder, ordering, paginate
from jobcrawler.common.schemas import (
    EducationLevel,
    EmploymentType,
    JobLevel,
    JobOut,
    JobStatus,
    WorkMode,
)
from jobcrawler.database.models import JOB_SALARY_MONTH_SQL, Company, Job, JobPosting, JobReview
from jobcrawler.modules.jobs.export_service import CSV_COLUMNS, csv_row
from jobcrawler.modules.jobs.status_service import status_filter
from jobcrawler.pipeline.provinces import PROVINCES
from jobcrawler.pipeline.store import job_to_out
from jobcrawler.services.resources import Resources

# --- truy vấn cho giao diện -----------------------------------------------------------------------

PROVINCE_NAMES = sorted(PROVINCES)


class JobItem(JobOut):
    """`JobOut` kèm id dòng `jobs` và kết quả kiểm tay (để giao diện/API dẫn link)."""

    id: UUID
    reviewed: bool | None = None  # None: chưa kiểm; True/False: đúng/sai


def job_item(row: Job, *, now: datetime, reviewed: bool | None = None) -> JobItem:
    out = job_to_out(row, now=now)
    return JobItem.model_validate({**out.model_dump(), "id": row.id, "reviewed": reviewed})


# Lương tháng quy về VND (lương năm / 12; theo giờ, ngoại tệ: NULL), cho bộ lọc `salary_from` và
# sắp theo lương — đúng chuỗi của index `ix_jobs_status_salary`.
SALARY_MONTH = literal_column(JOB_SALARY_MONTH_SQL, Float())

# Cột sắp xếp danh sách việc làm (`sortBy`) → (biểu thức, có NULL không). Mỗi cột có index
# (status, cột, id) theo chiều mặc định của giao diện (`models.Job`).
JobSort = Literal["first_seen_at", "posted_at", "salary", "expires_at"]
JOB_SORTS: dict[str, tuple[Any, bool]] = {
    "first_seen_at": (Job.first_seen_at, False),
    "posted_at": (Job.posted_at, True),
    "salary": (SALARY_MONTH, True),
    "expires_at": (Job.expires_at, True),
}


def job_ordering(sort_by: str, order: SortOrder) -> list[Any]:
    key, nullable = JOB_SORTS.get(sort_by, JOB_SORTS["first_seen_at"])
    return ordering(key, order, Job.id, nullable=nullable)


# Bộ lọc kinh nghiệm (`experience`) → khoảng số năm kinh nghiệm tối thiểu tin yêu cầu [từ, dưới).
EXPERIENCE_BUCKETS: dict[str, tuple[float | None, float | None]] = {
    "0": (None, 1),  # không yêu cầu / dưới 1 năm
    "1": (1, 2),
    "2": (2, 3),
    "3": (3, 5),  # 3–4 năm
    "5": (5, None),  # từ 5 năm
}

# Bộ lọc kiểm tay (`reviewed`): tất cả | chưa kiểm | đã kiểm | đã kiểm, đúng | đã kiểm, sai.
REVIEWED_FILTERS = ("", "no", "yes", "correct", "wrong")


class JobFilterQuery(BaseModel):
    """Bộ lọc việc làm (danh sách `GET /api/jobs`, xuất `GET /api/jobs/export`)."""

    search: str = Field(default="", description="Tìm trong tiêu đề / tên doanh nghiệp")
    province: str = ""
    category: str = Field(default="", description="Ngành nghề (một giá trị của meta.categories)")
    level: str = ""
    education: str = Field(default="", description="Học vấn tối thiểu (meta.educations)")
    experience: str = Field(
        default="",
        description="Kinh nghiệm tối thiểu tin yêu cầu: 0 (dưới 1 năm) | 1 | 2 | 3 (3–4 năm) | 5 "
        "(từ 5 năm)",
    )
    employment_type: str = ""
    work_mode: str = ""
    source: str = ""
    status: str = Field(
        default=JobStatus.ACTIVE.value,
        description="active (đang mở, chưa quá hạn nộp) | expired (kể cả job đang mở đã quá hạn "
        "nộp) | removed | all",
    )
    salary_from: float | None = Field(
        default=None,
        description="triệu VND / tháng (lương năm tính theo tháng; lương theo giờ, ngoại tệ không "
        "khớp)",
    )
    company_id: UUID | None = None
    reviewed: str = Field(default="", description="no | yes | correct | wrong")

    @field_validator("reviewed")
    @classmethod
    def _known_reviewed(cls, value: str) -> str:
        # Giá trị lạ → 422 (không thì bị hiểu là "sai"). Spec giữ kiểu chuỗi như các bộ lọc khác.
        if value not in REVIEWED_FILTERS:
            raise ValueError("reviewed: no | yes | correct | wrong")
        return value


class JobExportQuery(JobFilterQuery):
    """`GET /api/jobs/export`: bộ lọc + định dạng (mọi dòng, theo doanh nghiệp)."""

    fmt: str = Field(default="csv", pattern="^(csv|jsonl)$")


class JobListQuery(GetManyQuery, JobFilterQuery):
    """`GET /api/jobs`: bộ lọc + trang, số dòng, sắp xếp (`GetManyQuery`)."""

    sortBy: JobSort = Field(default="first_seen_at", description="Cột sắp xếp")


def job_query(f: JobFilterQuery, *, now: datetime) -> Select[Job]:
    """Bộ lọc → SELECT. `now`: mốc hạn nộp của bộ lọc trạng thái (`status_service`)."""
    stmt = select(Job)
    if search := f.search.strip():
        pattern = f"%{search}%"
        stmt = stmt.where(or_(Job.title.ilike(pattern), Job.company_name.ilike(pattern)))
    if f.province:
        stmt = stmt.where(Job.locations.contains([{"province": f.province}]))
    if f.category:
        stmt = stmt.where(Job.categories.contains([f.category]))
    if f.level:
        stmt = stmt.where(Job.level == f.level)
    if f.education:
        stmt = stmt.where(Job.education == f.education)
    if f.experience in EXPERIENCE_BUCKETS:
        low, high = EXPERIENCE_BUCKETS[f.experience]
        if low is not None:
            stmt = stmt.where(Job.experience_years_min >= low)
        if high is not None:
            stmt = stmt.where(Job.experience_years_min < high)
    if f.employment_type:
        stmt = stmt.where(Job.employment_type == f.employment_type)
    if f.work_mode:
        stmt = stmt.where(Job.work_mode == f.work_mode)
    if f.source:
        stmt = stmt.where(func.split_part(Job.canonical_source, ":", 1) == f.source)
    if (status := status_filter(f.status, now)) is not None:
        stmt = stmt.where(status)
    if f.salary_from:
        stmt = stmt.where(f.salary_from * 1_000_000 <= SALARY_MONTH)
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


async def list_jobs(session: AsyncSession, f: JobListQuery) -> Page[JobItem]:
    """Một trang việc làm. Mỗi cột sắp xếp có index (status, cột, id); tìm kiếm tiêu đề / tên
    doanh nghiệp dùng GIN trigram; tổng đếm tối đa COUNT_CAP."""
    review = select(JobReview.correct).where(JobReview.job_id == Job.id).scalar_subquery()
    now = datetime.now(UTC)
    return await paginate(
        session,
        job_query(f, now=now).add_columns(review),
        f.page,
        limit=f.limit,
        order_by=job_ordering(f.sortBy, f.sortOrder),
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


def enum_values(
    kind: type[JobLevel] | type[EmploymentType] | type[WorkMode] | type[EducationLevel],
) -> list[str]:
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


# --- thao tác cho API -----------------------------------------------------------------------------


async def export_stream(res: Resources, filters: JobFilterQuery, fmt: str) -> AsyncIterator[str]:
    """Job theo bộ lọc hiện tại, ghi dần (JSONL: mỗi dòng một JobOut; CSV có BOM cho Excel)."""
    now = datetime.now(UTC)
    async with res.sessionmaker() as session:
        stmt = job_query(filters, now=now).order_by(Job.company_id, Job.id)
        result = await session.stream_scalars(stmt.execution_options(yield_per=500))
        buffer = io.StringIO()
        writer: csv.DictWriter[str] | None = None
        if fmt == "csv":
            buffer.write("﻿")
            writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
            writer.writeheader()
        async for partition in result.partitions():
            for row in partition:
                job = job_to_out(row, now=now)
                if writer is not None:
                    writer.writerow(csv_row(job))
                else:
                    buffer.write(json.dumps(job.model_dump(mode="json"), ensure_ascii=False))
                    buffer.write("\n")
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate()
        if buffer.tell():
            yield buffer.getvalue()


async def job_exists(res: Resources, job_id: UUID) -> bool:
    async with res.sessionmaker() as session:
        return await session.scalar(select(Job.id).where(Job.id == job_id)) is not None
