"""Job từ website doanh nghiệp: tìm trang careers/ATS rồi thu thập (M2)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from jobcrawler.llm import LLMClient
from jobcrawler.logging import get_logger
from jobcrawler.net.http import Fetcher
from jobcrawler.net.kv import KeyValueStore, RedisKV
from jobcrawler.net.urls import parse_domain_input
from jobcrawler.pipeline.collect import CollectResult, collect
from jobcrawler.pipeline.discovery import DiscoveryResult, discover, sources_for
from jobcrawler.resources import Resources
from jobcrawler.schemas import RawJob
from jobcrawler.sources.base import FetchRecord, Source, SourceLimits, SourceSession

log = get_logger(__name__)

DISCOVERY_SOURCE = "discovery"
# Nguồn tìm được không qua website (profile trên trang tuyển dụng): chạy song song với bước tìm
# nguồn trên website, không phải chờ nó.
SourceFactory = Callable[[], Awaitable[Sequence[Source]]]


def merge_collected(a: CollectResult, b: CollectResult) -> CollectResult:
    """Gộp kết quả của hai lượt thu thập dùng chung danh sách `records`."""
    merged = CollectResult(jobs=[*a.jobs, *b.jobs], reports=[*a.reports, *b.reports])
    merged.records = a.records
    return merged


@dataclass(slots=True)
class WebsiteJobs:
    website: str
    domain: str
    discovery: DiscoveryResult | None
    collected: CollectResult = field(default_factory=CollectResult)
    incomplete_sources: list[str] = field(default_factory=list)

    @property
    def jobs(self) -> list[RawJob]:
        return self.collected.jobs

    @property
    def records(self) -> list[FetchRecord]:
        return self.collected.records


async def website_jobs(
    website: str | None,
    *,
    fetcher: Fetcher,
    llm: LLMClient,
    kv: KeyValueStore,
    deadline: float,
    limits: SourceLimits | None = None,
    refresh: bool = False,
    discover_only: bool = False,
    cache_ttl: float = 7 * 86_400,
    negative_ttl: float = 86_400,
    discovery_timeout: float | None = None,
    records: list[FetchRecord] | None = None,
    extra_sources: Sequence[Source] = (),
    board_sources: SourceFactory | None = None,
) -> WebsiteJobs:
    """Tìm nguồn trên `website` (domain hoặc URL) rồi thu thập song song tới `deadline`
    (`time.monotonic()`), cùng các nguồn có sẵn `extra_sources` (vd. profile trên trang tuyển
    dụng). `board_sources`: hàm trả thêm nguồn, được gọi và thu thập ngay trong lúc tìm nguồn
    trên website. Bước tìm nguồn dùng tối đa `discovery_timeout` giây; hết giờ thì thu thập từ
    những nguồn đã tìm được và ghi `discovery:<domain>` vào `incomplete_sources`. Không có
    website thì chỉ thu thập `extra_sources`."""
    limits = limits or SourceLimits()
    records = records if records is not None else []

    async def collect_boards() -> CollectResult:
        assert board_sources is not None
        found = await board_sources()
        return await collect(
            list(found), fetcher=fetcher, llm=llm, deadline=deadline, limits=limits, records=records
        )

    boards = asyncio.create_task(collect_boards()) if board_sources is not None else None
    try:
        out = await _website_jobs(
            website,
            fetcher=fetcher,
            llm=llm,
            kv=kv,
            deadline=deadline,
            limits=limits,
            refresh=refresh,
            discover_only=discover_only,
            cache_ttl=cache_ttl,
            negative_ttl=negative_ttl,
            discovery_timeout=discovery_timeout,
            records=records,
            extra_sources=extra_sources,
        )
        if boards is not None and not discover_only:
            board_result = await boards
            out.collected = merge_collected(out.collected, board_result)
            out.incomplete_sources += board_result.incomplete_sources
        return out
    finally:
        if boards is not None and not boards.done():
            boards.cancel()
            await asyncio.gather(boards, return_exceptions=True)


async def _website_jobs(
    website: str | None,
    *,
    fetcher: Fetcher,
    llm: LLMClient,
    kv: KeyValueStore,
    deadline: float,
    limits: SourceLimits,
    refresh: bool,
    discover_only: bool,
    cache_ttl: float,
    negative_ttl: float,
    discovery_timeout: float | None,
    records: list[FetchRecord],
    extra_sources: Sequence[Source],
) -> WebsiteJobs:
    if website is None:
        out = WebsiteJobs(website="", domain="", discovery=None)
        out.collected = await collect(
            list(extra_sources),
            fetcher=fetcher,
            llm=llm,
            deadline=deadline,
            limits=limits,
            records=records,
        )
        out.incomplete_sources = out.collected.incomplete_sources
        return out
    domain = parse_domain_input(website)
    out = WebsiteJobs(website=website, domain=domain, discovery=None)
    out.collected.records = records
    session = SourceSession(f"{DISCOVERY_SOURCE}:{domain}", fetcher, llm, limits, records)
    budget = max(0.0, deadline - time.monotonic())
    out.discovery = await discover(
        session,
        kv,
        website,
        refresh=refresh,
        cache_ttl=cache_ttl,
        negative_ttl=negative_ttl,
        timeout=min(budget, discovery_timeout) if discovery_timeout else budget,
    )
    if out.discovery.error is not None:
        # Hết giờ, trang chủ không mở được, bị chặn...: chưa chắc đã thấy mọi nguồn trên website,
        # nên lượt chạy không đủ để coi job cũ từ website là đã gỡ.
        log.warning(
            "discovery.incomplete",
            domain=domain,
            error=out.discovery.error,
            found=out.discovery.found,
        )
        out.incomplete_sources = [session.name]
    if discover_only:
        return out
    out.collected = await collect(
        [*sources_for(out.discovery), *extra_sources],
        fetcher=fetcher,
        llm=llm,
        deadline=deadline,
        limits=limits,
        records=records,
    )
    out.incomplete_sources += out.collected.incomplete_sources
    return out


async def jobs_for_website(
    res: Resources,
    website: str | None,
    *,
    deadline: float | None = None,
    refresh: bool = False,
    discover_only: bool = False,
    records: list[FetchRecord] | None = None,
    extra_sources: Sequence[Source] = (),
    board_sources: SourceFactory | None = None,
) -> WebsiteJobs:
    s = res.settings
    return await website_jobs(
        website,
        fetcher=res.http,
        llm=res.llm,
        kv=RedisKV(res.redis, prefix=s.redis_prefix),
        deadline=deadline or time.monotonic() + s.company_deadline_seconds,
        limits=SourceLimits.from_settings(s),
        refresh=refresh,
        discover_only=discover_only,
        cache_ttl=s.discovery_cache_seconds,
        negative_ttl=s.discovery_negative_cache_seconds,
        discovery_timeout=s.discovery_timeout_seconds,
        records=records,
        extra_sources=extra_sources,
        board_sources=board_sources,
    )
