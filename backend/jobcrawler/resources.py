"""Mở/đóng mọi tài nguyên dùng chung của một process backend (CLI, runner, scheduler, API).

Crawl không đi thẳng từ backend: `Resources.http` gửi từng request tới worker lấy trang qua Redis
Streams (`gateway.dispatch.FetchClient`); chỉ API bên ngoài (SERP) gọi thẳng (`api_http`, httpx).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

from redis.asyncio import Redis
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from jobcrawler import logging as jlog
from jobcrawler.config import Settings, get_settings
from jobcrawler.db import compress
from jobcrawler.db.session import create_engine, create_sessionmaker
from jobcrawler.gateway.dispatch import FetchClient
from jobcrawler.gateway.remote import RemoteRenderer, RemoteTransport
from jobcrawler.llm import LLMClient
from jobcrawler.net.direct import DirectTransport
from jobcrawler.net.http import HttpClient, HttpConfig
from jobcrawler.net.kv import KeyValueStore, RedisKV
from jobcrawler.net.proxy_pool import (
    Proxy,
    ProxyParseError,
    ProxyPool,
    load_proxies,
    parse_proxy_line,
)
from jobcrawler.net.ratelimit import RedisRateLimiter
from jobcrawler.queues import Queues

log = jlog.get_logger(__name__)


class ResourceUnavailableError(RuntimeError):
    """Không kết nối được Postgres/Redis lúc khởi động."""


@dataclass(slots=True)
class Resources:
    # Cài đặt hiện hành (env/.env + trang Cài đặt); được thay khi cài đặt đổi (live_settings).
    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    redis: Redis
    queues: Queues  # build, poll, boards
    llm: LLMClient  # `llm.enabled` False khi chưa cấu hình LLM_BASE_URL/LLM_MODEL
    http: HttpClient  # crawl: website, ATS, trang tuyển dụng (qua worker lấy trang)
    # API bên ngoài gọi thẳng từ backend (SERP): không qua worker, không proxy.
    api_http: HttpClient
    proxies: ProxyPool  # proxy chung (PROXY_MODE=central); rỗng ở chế độ khác
    fetch: FetchClient  # hàng việc lấy trang (`fetch.stats()`)


def redis_location(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.hostname}:{parts.port or 6379}"


def central_proxies(settings: Settings) -> list[Proxy]:
    """Danh sách proxy chung: PROXIES (trang Cài đặt) rồi PROXIES_FILE, bỏ trùng."""
    found: dict[str, Proxy] = {}
    for line in settings.proxies:
        try:
            proxy = parse_proxy_line(line)
        except ProxyParseError as exc:
            log.warning("proxy.invalid_line", source="PROXIES", error=str(exc))
            continue
        if proxy is not None:
            found.setdefault(proxy.id, proxy)
    for proxy in load_proxies(settings.proxies_file):
        found.setdefault(proxy.id, proxy)
    return list(found.values())


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
    db_logs: bool = True,
    live_settings: bool = True,
) -> AsyncIterator[Resources]:
    """Mở DB, Redis, queue job, LLM client, HTTP client (qua worker lấy trang: proxy pool, rate
    limit, robots, cache) và HTTP client gọi API bên ngoài.

    `db_logs`: gắn sink ghi log vào bảng `logs`. `live_settings`: áp giá trị sửa ở trang Cài đặt
    và nạp lại khi chúng đổi (`res.settings` được thay).
    """
    from jobcrawler.services.live_settings import LiveSettings

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
        sessionmaker = create_sessionmaker(engine)

        redis = Redis.from_url(settings.redis_url)
        stack.push_async_callback(redis.aclose)
        try:
            await redis.ping()  # pyright: ignore[reportGeneralTypeIssues]
        except Exception as exc:
            where = redis_location(settings.redis_url)
            raise ResourceUnavailableError(f"Không kết nối được Redis ({where}): {exc}") from exc

        live = LiveSettings(settings, sessionmaker, redis)
        if live_settings:
            settings = await live.load()

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

        queues = Queues.from_settings(settings)
        await queues.connect()
        stack.push_async_callback(queues.disconnect)

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
        if settings.proxy_mode != "central":
            log.info("proxy.central_off", proxy_mode=settings.proxy_mode)
        elif proxy_list := central_proxies(settings):
            log.info("proxy.loaded", count=len(proxy_list), file=str(settings.proxies_file))
        else:
            log.warning(
                "proxy.none",
                file=str(settings.proxies_file),
                reason="PROXY_MODE=central nhưng không có proxy chung nào: request đi thẳng",
            )
        proxies = make_proxy_pool(settings, kv, proxy_list)

        fetch = FetchClient(redis, settings)
        stack.push_async_callback(fetch.close)
        direct = settings.proxy_mode == "off"
        limiter = RedisRateLimiter(redis, prefix=settings.redis_prefix)
        http = HttpClient(
            RemoteTransport(fetch, direct=direct),
            kv=kv,
            proxies=proxies,
            ratelimiter=limiter,
            renderer=RemoteRenderer(fetch, direct=direct),
            config=HttpConfig.from_settings(settings),
        )
        stack.push_async_callback(http.close)
        api_http = HttpClient(
            DirectTransport(),
            kv=kv,
            proxies=make_proxy_pool(settings, kv, []),
            ratelimiter=limiter,
            config=HttpConfig.from_settings(settings),
        )
        stack.push_async_callback(api_http.close)

        res = Resources(
            settings=settings,
            engine=engine,
            sessionmaker=sessionmaker,
            redis=redis,
            queues=queues,
            llm=llm,
            http=http,
            api_http=api_http,
            proxies=proxies,
            fetch=fetch,
        )
        if live_settings:

            def apply(new: Settings) -> None:
                res.settings = new

            live.on_change = apply
            live.start()
            stack.push_async_callback(live.stop)
        yield res
