"""Thu thập song song mọi nguồn của một doanh nghiệp, dừng ở deadline.

Mỗi nguồn chạy trong một task; `asyncio.wait(timeout=còn lại tới deadline)`, nguồn chưa xong
bị huỷ và ghi vào `incomplete_sources`. Tin đã yield trước khi huỷ vẫn được giữ. Nguồn dừng
sớm vì chạm giới hạn (`SourceSession.truncate`) cũng tính là chưa xong.

`mode="build"`: mỗi nguồn ghi bộ khung của mình (`SourceReport.plan`); `mode="poll"`: nguồn đi
theo bộ khung truyền vào (`plans`, theo tên nguồn) và báo `broken` khi bộ khung không còn đúng.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

import structlog

from jobcrawler.common.logging import get_logger
from jobcrawler.common.schemas import FetchOutcome, RawJob
from jobcrawler.services.llm import LLMClient
from jobcrawler.services.net.http import Fetcher
from jobcrawler.sources.base import (
    FetchRecord,
    KnownJobs,
    Mode,
    Source,
    SourceError,
    SourceLimits,
    SourceSession,
)
from jobcrawler.sources.plan import PlanSpec

log = get_logger(__name__)


@dataclass(slots=True)
class SourceReport:
    name: str
    url: str
    outcome: FetchOutcome = FetchOutcome.OK
    jobs: int = 0
    error: str | None = None
    duration_ms: int = 0
    incomplete: bool = False  # bị huỷ ở deadline
    truncated: list[str] = field(default_factory=list)  # dừng sớm: chạm giới hạn, trang lỗi
    keys: list[str] = field(default_factory=list)  # mã các tin nguồn trả về (theo thứ tự)
    plan: PlanSpec | None = None  # build: bộ khung ghi được; poll: bộ khung đã đi theo
    broken: str | None = None  # poll: bộ khung không còn đúng

    @property
    def complete(self) -> bool:
        """Nguồn xong trọn: không lỗi, không bị huỷ, không bị cắt, bộ khung còn đúng."""
        return (
            self.outcome is FetchOutcome.OK
            and not self.incomplete
            and not self.truncated
            and self.broken is None
        )


@dataclass(slots=True)
class CollectResult:
    jobs: list[RawJob] = field(default_factory=list)
    reports: list[SourceReport] = field(default_factory=list)
    records: list[FetchRecord] = field(default_factory=list)

    @property
    def incomplete_sources(self) -> list[str]:
        return [r.name for r in self.reports if r.incomplete or r.truncated]


async def _run_source(
    source: Source,
    session: SourceSession,
    sink: list[RawJob],
    report: SourceReport,
    clock: Callable[[], float],
) -> None:
    started = clock()
    seen: set[str] = set()
    with structlog.contextvars.bound_contextvars(source=source.name):
        try:
            async for job in source.jobs(session):
                if job.external_id in seen:
                    continue
                seen.add(job.external_id)
                sink.append(job)
                report.keys.append(job.external_id)
                report.jobs += 1
                if report.jobs >= session.limits.max_jobs:
                    session.truncate("max_jobs")
                    break
        except SourceError as exc:
            report.outcome, report.error = exc.outcome, str(exc)
        except asyncio.CancelledError:
            report.outcome, report.error, report.incomplete = (
                FetchOutcome.TIMEOUT,
                "deadline",
                True,
            )
            raise
        except Exception as exc:
            report.outcome, report.error = FetchOutcome.ERROR, f"{type(exc).__name__}: {exc}"
            log.exception("source.error", url=source.url)
        finally:
            report.duration_ms = int((clock() - started) * 1000)
            report.truncated = list(session.truncated)
            report.broken = session.broken
            report.plan = session.plan
            if not session.polling:
                session.plan.jobs = report.jobs
            done = log.info if report.outcome is FetchOutcome.OK else log.warning
            done(
                "source.done",
                url=source.url,
                outcome=report.outcome.value,
                jobs=report.jobs,
                duration_ms=report.duration_ms,
                error=report.error,
                truncated=report.truncated or None,
            )


async def collect(
    sources: Sequence[Source],
    *,
    fetcher: Fetcher,
    llm: LLMClient,
    deadline: float,
    limits: SourceLimits | None = None,
    records: list[FetchRecord] | None = None,
    clock: Callable[[], float] = time.monotonic,
    known: KnownJobs | None = None,
    mode: Mode = "build",
    plans: Mapping[str, PlanSpec] | None = None,
) -> CollectResult:
    """Chạy song song mọi nguồn tới `deadline` (theo `clock`, mặc định `time.monotonic()`).

    `records`: danh sách nhận các `FetchRecord` (truyền vào để vẫn còn khi bị huỷ giữa chừng).
    `known`: tin đã tải chi tiết ở lượt gần đây, nguồn dùng lại thay vì tải lại.
    `mode`, `plans`: build (ghi bộ khung) hoặc poll (đi theo bộ khung của từng nguồn, theo tên).
    """
    limits = limits or SourceLimits()
    result = CollectResult(records=records if records is not None else [])
    sinks: list[list[RawJob]] = []
    tasks: dict[asyncio.Task[None], SourceReport] = {}
    for source in sources:
        spec = plans.get(source.name) if plans is not None else None
        own = limits
        if mode == "poll" and spec is not None and spec.pages > 0:
            # Phần trang của nguồn trong bộ khung (ATS phân trang như Workday đọc theo
            # `max_listing_pages`).
            own = dataclasses.replace(
                limits,
                poll_pages=spec.pages,
                max_listing_pages=min(limits.max_listing_pages, spec.pages),
            )
        session = SourceSession(source.name, fetcher, llm, own, result.records, known=known)
        session.mode = mode
        if spec is not None:
            session.plan = spec.model_copy(deep=True)
        report = SourceReport(name=source.name, url=source.url)
        sink: list[RawJob] = []
        sinks.append(sink)
        result.reports.append(report)
        task = asyncio.create_task(_run_source(source, session, sink, report, clock))
        tasks[task] = report

    if tasks:
        remaining = max(0.0, deadline - clock())
        try:
            _, pending = await asyncio.wait(tasks, timeout=remaining)
        finally:
            # Hết giờ, hoặc chính collect bị huỷ (timeout job): huỷ mọi nguồn còn chạy và chờ
            # chúng dừng hẳn để `finally` của từng nguồn kịp ghi báo cáo.
            pending = {t for t in tasks if not t.done()}
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        for task in pending:
            report = tasks[task]
            report.incomplete = True
            report.outcome = FetchOutcome.TIMEOUT
            report.error = report.error or "deadline"

    for sink in sinks:
        result.jobs.extend(sink)
    log.info(
        "collect.done",
        sources=len(result.reports),
        jobs=len(result.jobs),
        incomplete_sources=result.incomplete_sources,
    )
    return result
