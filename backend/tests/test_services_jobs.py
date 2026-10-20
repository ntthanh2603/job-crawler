from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from jobcrawler.common.schemas import ExtractionMethod, RawJob
from jobcrawler.modules.crawl.website import website_jobs
from jobcrawler.services.fakes import FakeChatClient, FakeFetcher
from jobcrawler.services.llm import LLMClient
from jobcrawler.services.net.http import FetchResult
from jobcrawler.services.net.kv import MemoryKV
from jobcrawler.sources.base import Source, SourceSession

FIXTURES = Path(__file__).parent / "fixtures" / "careers"
JOB = "https://careers.base.vn/job/hn-business-development-executive-junior-b2b-sales-1yoe-29675"


def base_fetcher() -> FakeFetcher:
    fetcher = FakeFetcher()
    for url, name in [
        ("https://base.vn/", "base_vn/home.html"),
        ("https://careers.base.vn/", "base_vn/careers.html"),
        ("https://careers.base.vn/alljobs", "base_vn/alljobs.html"),
        (JOB, "base_vn/job_29675.html"),
    ]:
        fetcher.add_file(url, FIXTURES / name)
    return fetcher


async def test_domain_to_jobs() -> None:
    """Đưa vào một domain thì ra danh sách job (M2)."""
    fetcher = base_fetcher()
    llm = LLMClient(FakeChatClient(["{}"]), model=None)
    found = await website_jobs(
        "base.vn", fetcher=fetcher, llm=llm, kv=MemoryKV(), deadline=time.monotonic() + 30
    )
    assert found.domain == "base.vn"
    assert found.discovery is not None
    assert found.discovery.careers_pages == ["https://careers.base.vn/"]
    assert [j.url for j in found.jobs] == [JOB]
    assert found.jobs[0].method is ExtractionMethod.JSONLD
    assert found.incomplete_sources == []
    sources = {r.source for r in found.records}
    assert sources == {"discovery:base.vn", "careers:careers.base.vn"}
    [report] = found.collected.reports
    assert (report.name, report.jobs) == ("careers:careers.base.vn", 1)


async def test_discover_only_and_deadline() -> None:
    llm = LLMClient(FakeChatClient(["{}"]), model=None)
    only = await website_jobs(
        "base.vn",
        fetcher=base_fetcher(),
        llm=llm,
        kv=MemoryKV(),
        deadline=time.monotonic() + 30,
        discover_only=True,
    )
    assert only.discovery is not None
    assert only.jobs == []
    assert only.collected.reports == []

    slow = base_fetcher()
    slow.delay = 0.1
    late = await website_jobs(
        "base.vn", fetcher=slow, llm=llm, kv=MemoryKV(), deadline=time.monotonic() + 0.25
    )
    assert late.discovery is not None
    assert late.discovery.error == "timeout"
    assert late.incomplete_sources[0] == "discovery:base.vn"  # (+ nguồn chưa kịp thu thập)
    assert late.records  # các lần fetch trước khi hết giờ vẫn được ghi

    # Hết ngân sách tìm nguồn nhưng đã có trang careers: vẫn thu thập.
    class StuckAfterCareers(FakeFetcher):
        async def fetch(self, url: str, **kwargs: Any) -> FetchResult:
            if "careers.base.vn" not in url and url != "https://base.vn/":
                await asyncio.sleep(30)  # ứng viên thứ hai, sitemap... treo
            return await super().fetch(url, **kwargs)

    stuck = StuckAfterCareers(results=base_fetcher().results)
    partial = await website_jobs(
        "base.vn",
        fetcher=stuck,
        llm=llm,
        kv=MemoryKV(),
        deadline=time.monotonic() + 30,
        discovery_timeout=0.3,
    )
    assert partial.discovery is not None
    assert partial.discovery.error == "timeout"
    assert partial.discovery.careers_pages == ["https://careers.base.vn/"]
    assert partial.incomplete_sources == ["discovery:base.vn"]
    assert [r.name for r in partial.collected.reports] == ["careers:careers.base.vn"]
    assert [j.url for j in partial.jobs] == [JOB]


class InstantBoard(Source):
    kind = "board"

    def __init__(self) -> None:
        super().__init__("7", "https://board.test/cong-ty/7")
        self.started: float | None = None

    async def jobs(self, session: SourceSession) -> AsyncIterator[RawJob]:
        self.started = time.monotonic()
        yield RawJob(
            source="fakeboard",
            external_id="1",
            url="https://board.test/viec-lam/ke-toan-1.html",
            title="Kế toán",
            method=ExtractionMethod.JSONLD,
        )


async def test_board_sources_are_collected_while_discovery_runs() -> None:
    board = InstantBoard()

    async def boards() -> list[Source]:
        return [board]

    slow = base_fetcher()
    slow.delay = 0.2  # mỗi request của bước tìm nguồn trên website mất 0,2 giây
    llm = LLMClient(FakeChatClient(["{}"]), model=None)
    started = time.monotonic()
    found = await website_jobs(
        "base.vn",
        fetcher=slow,
        llm=llm,
        kv=MemoryKV(),
        deadline=started + 30,
        board_sources=boards,
    )
    assert board.started is not None
    assert board.started - started < 0.2  # không chờ bước tìm nguồn
    assert sorted(r.name for r in found.collected.reports) == [
        "board:7",
        "careers:careers.base.vn",
    ]
    assert sorted(j.url for j in found.jobs) == sorted(
        [JOB, "https://board.test/viec-lam/ke-toan-1.html"]
    )
    assert found.incomplete_sources == []
