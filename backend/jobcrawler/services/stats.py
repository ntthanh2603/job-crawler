"""`jobcrawler stats`: queue, các lượt chạy, nguồn, index trang tuyển dụng, dung lượng."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Float, Select, cast, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from jobcrawler.db import compress
from jobcrawler.db.models import (
    BoardIndexRun,
    Company,
    CompanyRun,
    Job,
    JobPosting,
    SourceFetch,
    SourceProfile,
)
from jobcrawler.queues import JobQueue
from jobcrawler.resources import Resources
from jobcrawler.schemas import FetchOutcome, JobStatus, RunStatus

# Mục tiêu mặc định: tin mới thấy trong 3 phút ở P95, tỉ lệ lỗi dưới 2%.
TARGET_P95_SECONDS = 180.0
TARGET_FAILED_RATE = 0.02
TOP = 10


@dataclass(slots=True)
class QueueStats:
    name: str
    queued: int = 0
    active: int = 0
    scheduled: int = 0
    workers: int = 0
    # Tổng số job chạy cùng lúc tối đa của các runner đang nghe queue (RUNNER_*_CONCURRENCY, tự
    # tính theo máy khi 0): so với `active` để biết runner còn chỗ hay đã đầy.
    capacity: int = 0
    # Cộng dồn từ các worker đang sống (mỗi worker tự đếm từ lúc khởi động).
    complete: int = 0
    failed: int = 0
    retried: int = 0
    aborted: int = 0
    error: str | None = None


@dataclass(slots=True)
class RunStats:
    since: datetime | None
    runs: int = 0
    companies: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    # Theo lượt mới nhất của mỗi doanh nghiệp (lượt lỗi rồi chạy lại thành công thì tính là ok).
    latest_by_status: dict[str, int] = field(default_factory=dict)
    failed_rate: float | None = None
    p50_seconds: float | None = None
    p95_seconds: float | None = None
    max_seconds: float | None = None
    over_timeout: int = 0  # lượt dài hơn timeout cứng
    # Chờ trong hàng đợi (`started_at - queued_at`), chỉ lượt chạy từ queue.
    wait_p50_seconds: float | None = None
    wait_p95_seconds: float | None = None
    jobs_found: int = 0
    jobs_accepted: int = 0
    companies_with_jobs: int = 0
    first_started: datetime | None = None
    last_finished: datetime | None = None
    per_hour: float | None = None
    incomplete_sources: list[tuple[str, int]] = field(default_factory=list)
    errors: list[tuple[str, int]] = field(default_factory=list)


@dataclass(slots=True)
class SourceStats:
    kind: str
    fetches: int
    outcomes: dict[str, int]
    avg_ms: float | None
    cached: int = 0

    @property
    def blocked_rate(self) -> float:
        return (
            self.outcomes.get(FetchOutcome.BLOCKED.value, 0) / self.fetches if self.fetches else 0
        )


@dataclass(slots=True)
class BoardStats:
    source: str
    active_postings: int = 0
    postings: int = 0
    profiles: int = 0
    profiles_mapped: int = 0
    last_status: str | None = None
    last_started: datetime | None = None
    last_finished: datetime | None = None
    last_complete: bool | None = None
    last_urls_seen: int | None = None
    last_fetched: int | None = None
    last_new: int | None = None
    last_removed: int | None = None


@dataclass(slots=True)
class Target:
    name: str
    value: float | None
    target: str
    ok: bool | None
    kind: str = "seconds"  # seconds | rate


@dataclass(slots=True)
class Stats:
    generated_at: datetime
    queues: list[QueueStats]
    companies: int
    kyc_status: dict[str, int]
    jobs_by_status: dict[str, int]
    jobs_by_source: list[tuple[str, int]]
    runs: RunStats
    sources: list[SourceStats]
    boards: list[BoardStats]
    storage: list[compress.StorageStats]
    tables: list[tuple[str, int]]  # (bảng, byte trên đĩa gồm index và TOAST)
    targets: list[Target]


async def queue_stats(queue: JobQueue) -> QueueStats:
    st = QueueStats(name=queue.name)
    try:
        info = await queue.info()
    except Exception as exc:  # Redis lỗi không làm hỏng cả báo cáo
        st.error = str(exc)
        return st
    st.queued, st.active, st.scheduled = info["queued"], info["active"], info["scheduled"]
    st.workers = len(info["workers"])
    for worker in info["workers"].values():
        st.capacity += int(dict(worker.get("metadata") or {}).get("concurrency", 0))
        stats: dict[str, Any] = dict(worker.get("stats") or {})
        st.complete += int(stats.get("complete", 0))
        st.failed += int(stats.get("failed", 0))
        st.retried += int(stats.get("retried", 0))
        st.aborted += int(stats.get("aborted", 0))
    return st


def _window[*Ts](stmt: Select[*Ts], since: datetime | None) -> Select[*Ts]:
    return stmt.where(CompanyRun.started_at >= since) if since else stmt


async def run_stats(
    session: AsyncSession, since: datetime | None, *, timeout_seconds: float = 180
) -> RunStats:
    st = RunStats(since=since)
    finished = CompanyRun.status != RunStatus.RUNNING.value
    rows = await session.execute(
        _window(select(CompanyRun.status, func.count()).group_by(CompanyRun.status), since)
    )
    st.by_status = {status: n for status, n in rows}
    st.runs = sum(st.by_status.values())
    seconds = cast(CompanyRun.duration_ms, Float) / 1000
    agg = (
        await session.execute(
            _window(
                select(
                    func.count(func.distinct(CompanyRun.company_id)),
                    func.percentile_cont(0.5).within_group(seconds),
                    func.percentile_cont(0.95).within_group(seconds),
                    func.max(seconds),
                    func.coalesce(func.sum(CompanyRun.jobs_found), 0),
                    func.coalesce(func.sum(CompanyRun.jobs_accepted), 0),
                    func.min(CompanyRun.started_at),
                    func.max(CompanyRun.finished_at),
                ).where(finished),
                since,
            )
        )
    ).one()
    (
        st.companies,
        st.p50_seconds,
        st.p95_seconds,
        st.max_seconds,
        st.jobs_found,
        st.jobs_accepted,
        st.first_started,
        st.last_finished,
    ) = agg
    st.companies = int(st.companies or 0)
    st.jobs_found, st.jobs_accepted = int(st.jobs_found), int(st.jobs_accepted)
    if st.first_started and st.last_finished and st.last_finished > st.first_started:
        hours = (st.last_finished - st.first_started).total_seconds() / 3600
        done = st.runs - st.by_status.get(RunStatus.RUNNING.value, 0)
        st.per_hour = round(done / hours, 1) if hours > 0 else None
    over = select(func.count()).where(finished, CompanyRun.duration_ms > timeout_seconds * 1000)
    st.over_timeout = int(await session.scalar(_window(over, since)) or 0)
    wait = cast(func.extract("epoch", CompanyRun.started_at - CompanyRun.queued_at), Float)
    st.wait_p50_seconds, st.wait_p95_seconds = (
        await session.execute(
            _window(
                select(
                    func.percentile_cont(0.5).within_group(wait),
                    func.percentile_cont(0.95).within_group(wait),
                ).where(CompanyRun.queued_at.is_not(None)),
                since,
            )
        )
    ).one()

    # Lượt mới nhất (đã xong) của mỗi doanh nghiệp trong cửa sổ.
    latest = (
        _window(
            select(
                CompanyRun.company_id,
                CompanyRun.status,
                CompanyRun.jobs_accepted,
                func.row_number()
                .over(partition_by=CompanyRun.company_id, order_by=CompanyRun.started_at.desc())
                .label("rn"),
            ).where(finished),
            since,
        )
    ).subquery()
    rows = await session.execute(
        select(latest.c.status, func.count(), func.count().filter(latest.c.jobs_accepted > 0))
        .where(latest.c.rn == 1)
        .group_by(latest.c.status)
    )
    for status, n, with_jobs in rows:
        st.latest_by_status[status] = n
        st.companies_with_jobs += with_jobs
    total = sum(st.latest_by_status.values())
    if total:
        st.failed_rate = st.latest_by_status.get(RunStatus.FAILED.value, 0) / total

    source = func.split_part(func.unnest(CompanyRun.incomplete_sources), ":", 1)
    inner = _window(select(source.label("source")), since).subquery()
    rows = await session.execute(
        select(inner.c.source, func.count())
        .group_by(inner.c.source)
        .order_by(func.count().desc())
        .limit(TOP)
    )
    st.incomplete_sources = [(s, n) for s, n in rows]
    error = func.left(CompanyRun.error, 100)
    rows = await session.execute(
        _window(
            select(error, func.count())
            .where(CompanyRun.error.is_not(None))
            .group_by(error)
            .order_by(func.count().desc())
            .limit(TOP),
            since,
        )
    )
    st.errors = [(e, n) for e, n in rows]
    return st


async def source_stats(session: AsyncSession, since: datetime | None) -> list[SourceStats]:
    """Theo loại nguồn (phần trước dấu `:` của tên nguồn) trong các lượt chạy doanh nghiệp."""
    kind = func.split_part(SourceFetch.source, ":", 1)
    stmt = select(kind, SourceFetch.outcome, func.count(), func.avg(SourceFetch.duration_ms)).where(
        SourceFetch.run_id.is_not(None)
    )
    if since:
        stmt = stmt.where(SourceFetch.fetched_at >= since)
    rows = await session.execute(stmt.group_by(kind, SourceFetch.outcome))
    out: dict[str, SourceStats] = {}
    weighted: dict[str, float] = {}
    for name, outcome, n, avg in rows:
        st = out.setdefault(name, SourceStats(kind=name, fetches=0, outcomes={}, avg_ms=None))
        st.fetches += n
        st.outcomes[outcome] = n
        weighted[name] = weighted.get(name, 0.0) + float(avg or 0) * n
    for name, st in out.items():
        st.avg_ms = round(weighted[name] / st.fetches) if st.fetches else None
    return sorted(out.values(), key=lambda s: -s.fetches)


async def board_stats(session: AsyncSession) -> list[BoardStats]:
    out: dict[str, BoardStats] = {}

    def get(source: str) -> BoardStats:
        return out.setdefault(source, BoardStats(source=source))

    rows = await session.execute(
        select(
            JobPosting.source,
            func.count(),
            func.count().filter(JobPosting.status == JobStatus.ACTIVE.value),
        )
        .where(JobPosting.source_profile_id.is_not(None))
        .group_by(JobPosting.source)
    )
    for source, total, active in rows:
        get(source).postings, get(source).active_postings = total, active
    rows = await session.execute(
        select(
            SourceProfile.source,
            func.count(),
            func.count().filter(SourceProfile.company_id.is_not(None)),
        ).group_by(SourceProfile.source)
    )
    for source, total, mapped in rows:
        get(source).profiles, get(source).profiles_mapped = total, mapped
    last = (
        select(
            BoardIndexRun,
            func.row_number()
            .over(partition_by=BoardIndexRun.source, order_by=BoardIndexRun.started_at.desc())
            .label("rn"),
        )
    ).subquery()
    rows = await session.execute(select(last).where(last.c.rn == 1))
    for row in rows.mappings():
        st = get(row["source"])
        st.last_status = row["status"]
        st.last_started = row["started_at"]
        st.last_finished = row["finished_at"]
        st.last_complete = row["complete"]
        st.last_urls_seen = row["urls_seen"]
        st.last_fetched = row["fetched"]
        st.last_new = row["new"]
        st.last_removed = row["removed"]
    return sorted(out.values(), key=lambda b: -b.active_postings)


async def table_sizes(session: AsyncSession) -> list[tuple[str, int]]:
    rows = await session.execute(
        text(
            "SELECT relname, pg_total_relation_size(relid) FROM pg_stat_user_tables ORDER BY 2 DESC"
        )
    )
    return [(name, int(size)) for name, size in rows]


def targets(runs: RunStats) -> list[Target]:
    return [
        Target(
            "P95 thời gian mỗi doanh nghiệp",
            runs.p95_seconds,
            f"≤ {TARGET_P95_SECONDS:g} s",
            None if runs.p95_seconds is None else runs.p95_seconds <= TARGET_P95_SECONDS,
        ),
        Target(
            "Tỉ lệ thất bại (lượt mới nhất)",
            runs.failed_rate,
            f"< {TARGET_FAILED_RATE:.0%}",
            None if runs.failed_rate is None else runs.failed_rate < TARGET_FAILED_RATE,
            kind="rate",
        ),
    ]


async def collect_stats(res: Resources, *, hours: float | None = 24, storage: bool = True) -> Stats:
    now = datetime.now(UTC)
    since = now - timedelta(hours=hours) if hours else None
    queues = [await queue_stats(q) for q in res.queues.all()]
    async with res.sessionmaker() as session:
        companies = int(await session.scalar(select(func.count()).select_from(Company)) or 0)
        kyc = {
            (status or "none"): n
            for status, n in await session.execute(
                select(Company.kyc_status, func.count()).group_by(Company.kyc_status)
            )
        }
        jobs_by_status = {
            status: n
            for status, n in await session.execute(
                select(Job.status, func.count()).group_by(Job.status)
            )
        }
        # Cùng một biểu thức (cùng tham số bind) ở SELECT và GROUP BY.
        kind = func.split_part(Job.canonical_source, ":", 1)
        jobs_by_source = [
            (source, n)
            for source, n in await session.execute(
                select(kind, func.count())
                .where(Job.status == JobStatus.ACTIVE.value)
                .group_by(kind)
                .order_by(func.count().desc())
            )
        ]
        runs = await run_stats(session, since, timeout_seconds=res.settings.company_timeout_seconds)
        sources = await source_stats(session, since)
        boards = await board_stats(session)
        sizes = await table_sizes(session)
        packed = (
            [await compress.storage_stats(session, kind) for kind in compress.Kind]
            if storage
            else []
        )
    return Stats(
        generated_at=now,
        queues=queues,
        companies=companies,
        kyc_status=kyc,
        jobs_by_status=jobs_by_status,
        jobs_by_source=jobs_by_source,
        runs=runs,
        sources=sources,
        boards=boards,
        storage=packed,
        tables=sizes,
        targets=targets(runs),
    )
