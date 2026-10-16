from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator

from jobcrawler.fakes import FakeChatClient, FakeFetcher
from jobcrawler.llm import LLMClient
from jobcrawler.pipeline.collect import collect
from jobcrawler.schemas import ExtractionMethod, FetchOutcome, RawJob
from jobcrawler.sources.base import FetchRecord, Source, SourceError, SourceLimits, SourceSession


def job(source: str, n: int) -> RawJob:
    return RawJob(
        source=source,
        external_id=f"{source}:{n}",
        url=f"https://{source}.vn/jobs/{n}",
        title=f"Vị trí {n}",
        method=ExtractionMethod.ATS_API,
    )


class Scripted(Source):
    kind = "fake"

    def __init__(self, key: str, *, jobs: int = 2, then: str = "done", delay: float = 0) -> None:
        super().__init__(key, f"https://{key}.vn/")
        self.count, self.then, self.delay = jobs, then, delay

    async def jobs(self, session: SourceSession) -> AsyncIterator[RawJob]:
        await session.fetch(self.url)
        for n in range(self.count):
            yield job(self.key, n)
            yield job(self.key, n)  # trùng: bị bỏ
        if self.then == "sleep":
            await asyncio.sleep(self.delay)
            yield job(self.key, 99)
        elif self.then == "blocked":
            raise SourceError(FetchOutcome.BLOCKED, "captcha")
        elif self.then == "crash":
            raise RuntimeError("parser hỏng")


async def test_collect_runs_in_parallel_and_stops_at_deadline() -> None:
    fetcher = FakeFetcher()
    for key in ("ok", "slow", "blocked", "crash"):
        fetcher.add(f"https://{key}.vn/", "ok")
    records: list[FetchRecord] = []
    started = time.monotonic()
    result = await collect(
        [
            Scripted("ok"),
            Scripted("slow", jobs=1, then="sleep", delay=30),
            Scripted("blocked", jobs=1, then="blocked"),
            Scripted("crash", jobs=0, then="crash"),
        ],
        fetcher=fetcher,
        llm=LLMClient(FakeChatClient(["{}"]), model=None),
        deadline=time.monotonic() + 0.3,
        records=records,
    )
    assert time.monotonic() - started < 5

    by_name = {r.name: r for r in result.reports}
    assert by_name["fake:ok"].outcome is FetchOutcome.OK
    assert by_name["fake:ok"].jobs == 2
    assert by_name["fake:slow"].outcome is FetchOutcome.TIMEOUT
    assert by_name["fake:slow"].incomplete
    assert by_name["fake:slow"].jobs == 1  # tin yield trước deadline vẫn giữ
    assert by_name["fake:blocked"].outcome is FetchOutcome.BLOCKED
    assert by_name["fake:blocked"].error == "captcha"
    assert by_name["fake:crash"].outcome is FetchOutcome.ERROR
    assert "parser hỏng" in (by_name["fake:crash"].error or "")
    assert result.incomplete_sources == ["fake:slow"]
    assert sorted(j.external_id for j in result.jobs) == [
        "blocked:0",
        "ok:0",
        "ok:1",
        "slow:0",
    ]
    assert result.records is records
    assert sorted(r.source for r in records) == [
        "fake:blocked",
        "fake:crash",
        "fake:ok",
        "fake:slow",
    ]


async def test_collect_respects_max_jobs_and_empty_sources() -> None:
    fetcher = FakeFetcher()
    llm = LLMClient(FakeChatClient(["{}"]), model=None)
    result = await collect(
        [Scripted("big", jobs=10)],
        fetcher=fetcher,
        llm=llm,
        deadline=time.monotonic() + 5,
        limits=SourceLimits(max_jobs=3),
    )
    assert len(result.jobs) == 3
    assert result.reports[0].outcome is FetchOutcome.OK

    empty = await collect([], fetcher=fetcher, llm=llm, deadline=time.monotonic() + 5)
    assert empty.jobs == []
    assert empty.reports == []


async def test_collect_cancelled_from_outside_cancels_sources() -> None:
    fetcher = FakeFetcher()
    llm = LLMClient(FakeChatClient(["{}"]), model=None)
    task = asyncio.create_task(
        collect(
            [Scripted("slow", jobs=0, then="sleep", delay=30)],
            fetcher=fetcher,
            llm=llm,
            deadline=time.monotonic() + 60,
        )
    )
    await asyncio.sleep(0.1)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert all(t.done() for t in asyncio.all_tasks() if t is not asyncio.current_task())
