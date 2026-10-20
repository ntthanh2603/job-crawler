"""Truy vấn lượt chạy cho giao diện: chi tiết, request, log, trang parse lỗi."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jobcrawler.common.pagination import Page, capped_count, paginate
from jobcrawler.common.schemas import RunStatus
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


RUN_STATUSES = [s.value for s in RunStatus]
