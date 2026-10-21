"""Mở/đóng mọi tài nguyên dùng chung của một process backend (CLI, runner, scheduler, API).

Crawl không đi thẳng từ backend: `Resources.http` gửi từng request tới worker lấy trang qua Redis
Streams (`modules.gateway.dispatch_service.FetchClient`); chỉ API bên ngoài (SERP) gọi thẳng
(`api_http`, httpx).

Hai vai Redis:

- `redis` (REDIS_URL, phải noeviction + lưu đĩa): hàng đợi job (streamqueue, tự mở client theo
  REDIS_URL), stream / trả lời / registry / inflight của gateway lấy trang, khoá doanh nghiệp, rate
  limit theo domain và của LLM (token bucket + tạm dừng sau 429: bị đẩy ra là gửi dồn tới site),
  trạng thái proxy pool, phiên bản cài đặt, số đo SLA poll, chặn đoán mật khẩu, heartbeat health.
- `cache_redis` (CACHE_REDIS_URL; trống = cùng client `redis`): chỉ cache tính lại được — trang
  HTTP (`http:`), robots.txt, kết quả tìm nguồn, SERP, câu trả lời LLM (`cache_kv`, cũng là kv của
  `http` / `api_http` / `llm`) và số liệu trang tổng quan (`overview:`). Đặt được maxmemory +
  allkeys-lru; mất khoá chỉ làm chậm / tốn thêm request.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

from redis.asyncio import Redis
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from jobcrawler.common import logging as jlog
from jobcrawler.common.config import Settings, get_settings
from jobcrawler.database import compress
from jobcrawler.database.session import create_engine, create_sessionmaker
from jobcrawler.modules.gateway.dispatch_service import FetchClient
from jobcrawler.modules.gateway.remote_service import RemoteRenderer, RemoteTransport
from jobcrawler.services import secretbox
from jobcrawler.services.llm import LLMClient
from jobcrawler.services.net.direct import DirectTransport
from jobcrawler.services.net.http import HttpClient, HttpConfig
from jobcrawler.services.net.kv import KeyValueStore, RedisKV
from jobcrawler.services.net.proxy_pool import Proxy, ProxyPool
from jobcrawler.services.net.proxy_pool import central_proxies as proxy_list
from jobcrawler.services.net.ratelimit import RedisRateLimiter
from jobcrawler.services.redis.queues import Queues

log = jlog.get_logger(__name__)


class ResourceUnavailableError(RuntimeError):
    """Không kết nối được Postgres/Redis lúc khởi động."""


@dataclass(slots=True)
class Resources:
    # Cài đặt hiện hành (env/.env + trang Cài đặt); được thay khi cài đặt đổi (live_settings).
    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    redis: Redis  # Redis chính (REDIS_URL): hàng đợi, gateway, khoá, rate limit, số đo, health
    queues: Queues  # build, poll, boards
    llm: LLMClient  # `llm.enabled` False khi chưa cấu hình LLM_BASE_URL/LLM_MODEL
    http: HttpClient  # crawl: website, ATS, trang tuyển dụng (qua worker lấy trang)
    # API bên ngoài gọi thẳng từ backend (SERP): không qua worker, không proxy.
    api_http: HttpClient
    proxies: ProxyPool  # proxy chung (PROXY_MODE=central); rỗng ở chế độ khác
    fetch: FetchClient  # hàng việc lấy trang (`fetch.stats()`)
    # Redis cache (CACHE_REDIS_URL, trống = chính `redis`) và kv trên nó (khoá `<REDIS_PREFIX>:…`).
    cache_redis: Redis
    cache_kv: KeyValueStore


@dataclass(slots=True)
class Network:
    """Phần tầng mạng dựng từ hai client Redis (`wire_network`)."""

    cache_kv: KeyValueStore  # cache: trang HTTP, robots, tìm nguồn, SERP, LLM
    state_kv: KeyValueStore  # trạng thái dùng chung trên Redis chính: proxy pool
    llm: LLMClient
    proxies: ProxyPool
    http: HttpClient
    api_http: HttpClient


def redis_location(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.hostname}:{parts.port or 6379}"


def central_proxies(settings: Settings) -> list[Proxy]:
    """Danh sách proxy chung: PROXIES (trang Cài đặt) rồi PROXIES_FILE, bỏ trùng."""
    return proxy_list(settings.proxies, settings.proxies_file)


def open_redis(settings: Settings) -> tuple[Redis, Redis]:
    """(Redis chính, Redis cache). CACHE_REDIS_URL trống / trùng REDIS_URL: cùng một client."""
    redis = Redis.from_url(settings.redis_url)
    url = settings.effective_cache_redis_url
    return redis, redis if url == settings.redis_url else Redis.from_url(url)


def make_proxy_pool(settings: Settings, kv: KeyValueStore, proxies: list[Proxy]) -> ProxyPool:
    return ProxyPool(
        kv,
        proxies,
        sticky_seconds=settings.proxy_sticky_seconds,
        cooldown_seconds=settings.proxy_cooldown_seconds,
        max_failures=settings.proxy_max_consecutive_failures,
        dead_seconds=settings.proxy_dead_seconds,
    )


def wire_network(
    settings: Settings, *, redis: Redis, cache_redis: Redis, fetch: FetchClient
) -> Network:
    """LLM client, proxy pool và hai HTTP client: cache (trang, robots, LLM, SERP qua `cache_kv`)
    trên `cache_redis`; rate limit và trạng thái proxy trên `redis` (chính)."""
    state_kv = RedisKV(redis, prefix=settings.redis_prefix)
    cache_kv = RedisKV(cache_redis, prefix=settings.redis_prefix)
    # Giới hạn request/phút của endpoint LLM dùng chung mọi worker (token bucket trên Redis chính);
    # cache câu trả lời theo nội dung trên Redis cache.
    limiter = RedisRateLimiter(redis, prefix=settings.redis_prefix)
    llm = LLMClient.from_settings(settings, limiter=limiter, cache=cache_kv)

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
    proxies = make_proxy_pool(settings, state_kv, proxy_list)

    direct = settings.proxy_mode == "off"
    http = HttpClient(
        RemoteTransport(fetch, direct=direct),
        kv=cache_kv,
        proxies=proxies,
        ratelimiter=limiter,
        renderer=RemoteRenderer(fetch, direct=direct),
        config=HttpConfig.from_settings(settings),
    )
    api_http = HttpClient(
        DirectTransport(),
        kv=cache_kv,
        proxies=make_proxy_pool(settings, state_kv, []),
        ratelimiter=limiter,
        config=HttpConfig.from_settings(settings),
    )
    return Network(
        cache_kv=cache_kv,
        state_kv=state_kv,
        llm=llm,
        proxies=proxies,
        http=http,
        api_http=api_http,
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
    from jobcrawler.modules.settings.live_settings_service import LiveSettings

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

        redis, cache_redis = open_redis(settings)
        stack.push_async_callback(redis.aclose)
        try:
            await redis.ping()  # pyright: ignore[reportGeneralTypeIssues]
        except Exception as exc:
            where = redis_location(settings.redis_url)
            raise ResourceUnavailableError(f"Không kết nối được Redis ({where}): {exc}") from exc
        if cache_redis is not redis:
            stack.push_async_callback(cache_redis.aclose)
            try:
                await cache_redis.ping()  # pyright: ignore[reportGeneralTypeIssues]
            except Exception as exc:
                where = redis_location(settings.effective_cache_redis_url)
                raise ResourceUnavailableError(
                    f"Không kết nối được Redis cache ({where}): {exc}"
                ) from exc

        # Khoá giải mã trường nhạy cảm của trang Cài đặt (trước khi đọc bảng app_settings).
        await secretbox.ensure(settings, redis)
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

        fetch = FetchClient(redis, settings)
        stack.push_async_callback(fetch.close)
        net = wire_network(settings, redis=redis, cache_redis=cache_redis, fetch=fetch)
        stack.push_async_callback(net.llm.close)
        stack.push_async_callback(net.http.close)
        stack.push_async_callback(net.api_http.close)
        if not net.llm.enabled:
            log.warning("llm.disabled", reason="thiếu LLM_BASE_URL hoặc LLM_MODEL")
        if not settings.serp_enabled:
            log.warning("serp.disabled", reason="thiếu SERP_API_KEY: KYC bỏ qua bước tìm kiếm")

        res = Resources(
            settings=settings,
            engine=engine,
            sessionmaker=sessionmaker,
            redis=redis,
            queues=queues,
            llm=net.llm,
            http=net.http,
            api_http=net.api_http,
            proxies=net.proxies,
            fetch=fetch,
            cache_redis=cache_redis,
            cache_kv=net.cache_kv,
        )
        if live_settings:

            def apply(new: Settings) -> None:
                res.settings = new

            live.on_change = apply
            live.start()
            stack.push_async_callback(live.stop)
        yield res
