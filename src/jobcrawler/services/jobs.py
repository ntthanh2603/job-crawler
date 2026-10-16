"""Job từ website doanh nghiệp: tìm trang careers/ATS rồi thu thập (M2)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from jobcrawler.llm import LLMClient
from jobcrawler.logging import get_logger
from jobcrawler.net.http import Fetcher
from jobcrawler.net.kv import KeyValueStore, RedisKV
from jobcrawler.net.urls import parse_domain_input
from jobcrawler.pipeline.collect import CollectResult, collect
from jobcrawler.pipeline.discovery import TIMEOUT, DiscoveryResult, discover, sources_for
from jobcrawler.resources import Resources
from jobcrawler.schemas import RawJob
from jobcrawler.sources.base import FetchRecord, SourceLimits, SourceSession

log = get_logger(__name__)

DISCOVERY_SOURCE = "discovery"


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
    website: str,
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
) -> WebsiteJobs:
    """Tìm nguồn trên `website` (domain hoặc URL) rồi thu thập song song tới `deadline`
    (`time.monotonic()`). Bước tìm nguồn dùng tối đa `discovery_timeout` giây; hết giờ thì
    thu thập từ những nguồn đã tìm được và ghi `discovery:<domain>` vào `incomplete_sources`."""
    limits = limits or SourceLimits()
    records = records if records is not None else []
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
    if out.discovery.error == TIMEOUT:
        log.warning("discovery.timeout", domain=domain, found=out.discovery.found)
        out.incomplete_sources = [session.name]
    if discover_only:
        return out
    out.collected = await collect(
        sources_for(out.discovery),
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
    website: str,
    *,
    deadline: float | None = None,
    refresh: bool = False,
    discover_only: bool = False,
    records: list[FetchRecord] | None = None,
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
    )
