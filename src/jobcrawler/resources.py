"""Mở/đóng mọi tài nguyên dùng chung của một process (CLI, worker, sau này là API)."""

from __future__ import annotations

import sys
import warnings
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

from curl_cffi.utils import CurlCffiWarning
from redis.asyncio import Redis
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from jobcrawler import logging as jlog
from jobcrawler.config import Settings, get_settings
from jobcrawler.db import compress
from jobcrawler.db.session import create_engine, create_sessionmaker
from jobcrawler.llm import LLMClient
from jobcrawler.net.browser import BrowserManager
from jobcrawler.net.http import CurlTransport, HttpClient, HttpConfig
from jobcrawler.net.kv import KeyValueStore, RedisKV
from jobcrawler.net.proxy_pool import Proxy, ProxyPool, load_proxies
from jobcrawler.net.ratelimit import RedisRateLimiter
from jobcrawler.queues import JobQueue, make_queue

log = jlog.get_logger(__name__)

if sys.platform == "win32":
    # Playwright cần ProactorEventLoop; curl_cffi tự chạy selector ở thread phụ, đúng ý muốn.
    warnings.filterwarnings("ignore", message=r"\s*Proactor event loop", category=CurlCffiWarning)


class ResourceUnavailableError(RuntimeError):
    """Không kết nối được Postgres/Redis lúc khởi động."""


@dataclass(frozen=True, slots=True)
class Resources:
    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    redis: Redis
    queue: JobQueue
    llm: LLMClient  # `llm.enabled` False khi chưa cấu hình LLM_BASE_URL/LLM_MODEL
    http: HttpClient
    proxies: ProxyPool
    browser: BrowserManager


def _redis_location(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.hostname}:{parts.port or 6379}"


def make_proxy_pool(settings: Settings, kv: KeyValueStore, proxies: list[Proxy]) -> ProxyPool:
    return ProxyPool(
        kv,
        proxies,
        sticky_seconds=settings.proxy_sticky_seconds,
        cooldown_seconds=settings.proxy_cooldown_seconds,
        max_failures=settings.proxy_max_consecutive_failures,
        dead_seconds=settings.proxy_dead_seconds,
    )


async def _close_sink(sink: jlog.PgLogSink) -> None:
    jlog.detach_sink(sink)
    await sink.close()


@asynccontextmanager
async def open_resources(
    settings: Settings | None = None,
    *,
    queue: JobQueue | None = None,
    db_logs: bool = True,
) -> AsyncIterator[Resources]:
    """Mở DB, Redis, queue job, LLM client, HTTP client (proxy pool, rate limit, robots,
    cache) và browser (khởi động lười).

    `queue`: dùng queue có sẵn (worker tự quản lý kết nối của queue này).
    `db_logs`: gắn sink ghi log vào bảng `logs`.
    """
    settings = settings or get_settings()
    async with AsyncExitStack() as stack:
        engine = create_engine(settings)
        stack.push_async_callback(engine.dispose)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:
            where = make_url(settings.database_url).render_as_string(hide_password=True)
            raise ResourceUnavailableError(f"Không kết nối được Postgres ({where}): {exc}") from exc

        # Từ điển nén các cột lớn: nạp trước mọi truy vấn, làm mới định kỳ.
        compress.CODEC.set_level(settings.compression_level)
        refresher = compress.DictionaryRefresher(
            engine, interval=settings.compression_dict_refresh_seconds
        )
        await refresher.start()
        stack.push_async_callback(refresher.stop)

        if db_logs:
            sink = jlog.PgLogSink(jlog.make_copy_writer(engine), min_level=settings.log_db_level)
            sink.start()
            jlog.attach_sink(sink)
            stack.push_async_callback(_close_sink, sink)

        redis = Redis.from_url(settings.redis_url)
        stack.push_async_callback(redis.aclose)
        try:
            await redis.ping()  # pyright: ignore[reportGeneralTypeIssues]
        except Exception as exc:
            where = _redis_location(settings.redis_url)
            raise ResourceUnavailableError(f"Không kết nối được Redis ({where}): {exc}") from exc

        if queue is None:
            queue = make_queue(settings, settings.queue_name)
            await queue.connect()
            stack.push_async_callback(queue.disconnect)

        kv = RedisKV(redis, prefix=settings.redis_prefix)
        # Giới hạn request/phút của endpoint LLM dùng chung mọi worker (token bucket trên Redis);
        # cache câu trả lời theo nội dung trên Redis.
        llm = LLMClient.from_settings(
            settings, limiter=RedisRateLimiter(redis, prefix=settings.redis_prefix), cache=kv
        )
        stack.push_async_callback(llm.close)
        if not llm.enabled:
            log.warning("llm.disabled", reason="thiếu LLM_BASE_URL hoặc LLM_MODEL")
        if not settings.serp_enabled:
            log.warning("serp.disabled", reason="thiếu SERP_API_KEY: KYC bỏ qua bước tìm kiếm")

        proxy_list: list[Proxy] = []
        if not settings.proxy_enabled:
            log.info("proxy.disabled", reason="PROXY_ENABLED=false: đi thẳng, không proxy")
        elif proxy_list := load_proxies(settings.proxies_file):
            log.info("proxy.loaded", count=len(proxy_list), file=str(settings.proxies_file))
        else:
            log.warning(
                "proxy.none",
                file=str(settings.proxies_file),
                reason="không có proxy nên đi thẳng; đặt PROXY_ENABLED=false nếu cố ý",
            )
        proxies = make_proxy_pool(settings, kv, proxy_list)

        browser = BrowserManager(max_pages=settings.browser_max_pages, use_proxies=proxies.enabled)
        stack.push_async_callback(browser.close)

        http = HttpClient(
            CurlTransport(impersonate=settings.http_impersonate),
            kv=kv,
            proxies=proxies,
            ratelimiter=RedisRateLimiter(redis, prefix=settings.redis_prefix),
            renderer=browser,
            config=HttpConfig.from_settings(settings),
        )
        stack.push_async_callback(http.close)

        yield Resources(
            settings=settings,
            engine=engine,
            sessionmaker=create_sessionmaker(engine),
            redis=redis,
            queue=queue,
            llm=llm,
            http=http,
            proxies=proxies,
            browser=browser,
        )
