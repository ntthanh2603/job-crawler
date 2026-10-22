"""Truy vấn lượt build / poll cho giao diện: chi tiết, request, log, trang parse lỗi."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from pydantic import Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from jobcrawler.common.pagination import (
    GetManyQuery,
    Page,
    SortOrder,
    capped_count,
    ordering,
    paginate,
)
from jobcrawler.common.schemas import RunKind, RunStatus
from jobcrawler.database.models import Company, CompanyRun, FailedPage, Log, SourceFetch


@dataclass(slots=True)
class RunDetail:
    run: CompanyRun
    company: Company | None
    fetches: int
    logs: int
    failed_pages: list[FailedPage]
    by_source: list[tuple[str, int, dict[str, int]]] = field(default_factory=list)


async def run_detail(session: AsyncSession, run_id: uuid.UUID) -> RunDetail | None:
    """Tóm tắt một lượt build / poll; request và log lấy theo trang (`run_fetches`, `run_logs`)."""
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
    logs, _ = await capped_count(session, select(Log.id).where(Log.run_id == run_id))
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


# `created_at`: thứ tự gửi request (id uuid7).
FETCH_SORTS: dict[str, tuple[Any, bool]] = {
    "created_at": (SourceFetch.id, False),
    "duration_ms": (SourceFetch.duration_ms, True),
    "http_status": (SourceFetch.http_status, True),
    "items": (SourceFetch.items, True),
}
LOG_SORTS: dict[str, tuple[Any, bool]] = {
    "ts": (Log.ts, False),
    "level": (Log.level, False),
}


class RunFetchesQuery(GetManyQuery):
    """`GET /api/runs/{id}/fetches`: `search` trong URL / nguồn."""

    sortBy: Literal["created_at", "duration_ms", "http_status", "items"] = "created_at"
    sortOrder: SortOrder = Field(default=SortOrder.ASC, description="Chiều sắp xếp theo `sortBy`")


class RunLogsQuery(GetManyQuery):
    """`GET /api/runs/{id}/logs`: `search` trong tên sự kiện, `level` lọc đúng mức."""

    level: str = Field(default="", description="debug | info | warning | error | critical")
    sortBy: Literal["ts", "level"] = "ts"
    sortOrder: SortOrder = Field(default=SortOrder.ASC, description="Chiều sắp xếp theo `sortBy`")


async def run_fetches(
    session: AsyncSession, run_id: uuid.UUID, f: RunFetchesQuery | None = None
) -> Page[SourceFetch]:
    f = f or RunFetchesQuery()
    stmt = select(SourceFetch).where(SourceFetch.run_id == run_id)
    if search := f.search.strip():
        pattern = f"%{search}%"
        stmt = stmt.where(or_(SourceFetch.url.ilike(pattern), SourceFetch.source.ilike(pattern)))
    key, nullable = FETCH_SORTS[f.sortBy]
    return await paginate(
        session,
        stmt,
        f.page,
        limit=f.limit,
        order_by=ordering(key, f.sortOrder, SourceFetch.id, nullable=nullable),
    )


async def run_logs(
    session: AsyncSession, run_id: uuid.UUID, f: RunLogsQuery | None = None
) -> Page[Log]:
    f = f or RunLogsQuery()
    stmt = select(Log).where(Log.run_id == run_id)
    if search := f.search.strip():
        stmt = stmt.where(Log.event.ilike(f"%{search}%"))
    if f.level:
        stmt = stmt.where(Log.level == f.level)
    key, nullable = LOG_SORTS[f.sortBy]
    return await paginate(
        session,
        stmt,
        f.page,
        limit=f.limit,
        order_by=ordering(key, f.sortOrder, Log.id, nullable=nullable),
    )


@dataclass(slots=True)
class HourBucket:
    hour: datetime
    counts: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.counts.values())


async def runs_per_hour(session: AsyncSession, *, hours: int = 24) -> list[HourBucket]:
    """Số lượt build / poll theo giờ (theo trạng thái) trong `hours` giờ qua, đủ cả giờ không có
    lượt."""
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


async def recent_runs(session: AsyncSession, limit: int = 15) -> list[tuple[CompanyRun, str]]:
    rows = await session.execute(
        select(CompanyRun, Company.input_name)
        .join(Company, Company.id == CompanyRun.company_id)
        .order_by(CompanyRun.started_at.desc())
        .limit(limit)
    )
    return [(run, name) for run, name in rows]


async def failed_page(session: AsyncSession, page_id: UUID) -> FailedPage | None:
    return await session.get(FailedPage, page_id)


async def run_kind(session: AsyncSession, run_id: UUID | None) -> RunKind | None:
    """Lượt `run_id` là build hay poll (None: không có / đã quá hạn lưu)."""
    if run_id is None:
        return None
    kind = await session.scalar(select(CompanyRun.kind).where(CompanyRun.run_id == run_id))
    return RunKind(kind) if kind is not None else None


RUN_STATUSES = [s.value for s in RunStatus]
