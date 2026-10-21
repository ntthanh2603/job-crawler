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

from pydantic import BaseModel, Field
from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from jobcrawler.common.pagination import GetManyQuery, Page, SortOrder, ordering, paginate
from jobcrawler.common.schemas import EmploymentType, JobLevel, JobOut, JobStatus, WorkMode
from jobcrawler.database.models import Company, Job, JobPosting, JobReview
from jobcrawler.modules.jobs.export_service import CSV_COLUMNS, csv_row
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


# Cột sắp xếp danh sách việc làm (`sortBy`) → (biểu thức, có NULL không). Mỗi cột có index
# (status, cột, id) theo chiều mặc định của giao diện (`models.Job`).
JobSort = Literal["first_seen_at", "posted_at", "salary", "expires_at"]
JOB_SORTS: dict[str, tuple[Any, bool]] = {
    "first_seen_at": (Job.first_seen_at, False),
    "posted_at": (Job.posted_at, True),
    "salary": (func.coalesce(Job.salary_max, Job.salary_min), True),
    "expires_at": (Job.expires_at, True),
}


def job_ordering(sort_by: str, order: SortOrder) -> list[Any]:
    key, nullable = JOB_SORTS.get(sort_by, JOB_SORTS["first_seen_at"])
    return ordering(key, order, Job.id, nullable=nullable)


class JobFilterQuery(BaseModel):
    """Bộ lọc việc làm (danh sách `GET /api/jobs`, xuất `GET /api/jobs/export`)."""

    search: str = Field(default="", description="Tìm trong tiêu đề / tên doanh nghiệp")
    province: str = ""
    level: str = ""
    employment_type: str = ""
    work_mode: str = ""
    source: str = ""
    status: str = Field(
        default=JobStatus.ACTIVE.value, description="active | expired | removed | all"
    )
    salary_from: float | None = Field(default=None, description="triệu VND / tháng")
    company_id: UUID | None = None
    reviewed: str = Field(default="", description="no | yes | correct | wrong")


class JobExportQuery(JobFilterQuery):
    """`GET /api/jobs/export`: bộ lọc + định dạng (mọi dòng, theo doanh nghiệp)."""

    fmt: str = Field(default="csv", pattern="^(csv|jsonl)$")


class JobListQuery(GetManyQuery, JobFilterQuery):
    """`GET /api/jobs`: bộ lọc + trang, số dòng, sắp xếp (`GetManyQuery`)."""

    sortBy: JobSort = Field(default="first_seen_at", description="Cột sắp xếp")


def job_query(f: JobFilterQuery) -> Select[Job]:
    stmt = select(Job)
    if search := f.search.strip():
        pattern = f"%{search}%"
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
    if f.status and f.status != "all":
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


async def list_jobs(session: AsyncSession, f: JobListQuery) -> Page[JobItem]:
    """Một trang việc làm. Mỗi cột sắp xếp có index (status, cột, id); tìm kiếm tiêu đề / tên
    doanh nghiệp dùng GIN trigram; tổng đếm tối đa COUNT_CAP."""
    review = select(JobReview.correct).where(JobReview.job_id == Job.id).scalar_subquery()
    now = datetime.now(UTC)
    return await paginate(
        session,
        job_query(f).add_columns(review),
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


# --- thao tác cho API -----------------------------------------------------------------------------


async def export_stream(res: Resources, filters: JobFilterQuery, fmt: str) -> AsyncIterator[str]:
    """Job theo bộ lọc hiện tại, ghi dần (JSONL: mỗi dòng một JobOut; CSV có BOM cho Excel)."""
    now = datetime.now(UTC)
    async with res.sessionmaker() as session:
        stmt = job_query(filters).order_by(Job.company_id, Job.id)
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
