"""Mở/đóng mọi tài nguyên dùng chung của một process (CLI, worker, sau này là API)."""

from __future__ import annotations

import sys
import warnings
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlsplit

from curl_cffi.requests import AsyncSession as CurlSession
from curl_cffi.utils import CurlCffiWarning
from openai import AsyncOpenAI
from redis.asyncio import Redis
from saq import Queue
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from jobcrawler import logging as jlog
from jobcrawler.config import Settings, get_settings
from jobcrawler.db.session import create_engine, create_sessionmaker
from jobcrawler.net.browser import BrowserManager

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
    queue: Queue
    llm: AsyncOpenAI | None  # None khi chưa cấu hình LLM_BASE_URL/LLM_MODEL
    http: CurlSession
    browser: BrowserManager


def _redis_location(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.hostname}:{parts.port or 6379}"


async def _close_sink(sink: jlog.PgLogSink) -> None:
    jlog.detach_sink(sink)
    await sink.close()


@asynccontextmanager
async def open_resources(
    settings: Settings | None = None,
    *,
    queue: Queue | None = None,
    db_logs: bool = True,
) -> AsyncIterator[Resources]:
    """Mở DB, Redis, SAQ queue, LLM client, HTTP client và browser (lười).

    `queue`: dùng queue có sẵn (worker SAQ tự quản lý kết nối của queue này).
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
            queue = Queue.from_url(settings.redis_url, name=settings.queue_name)
            await queue.connect()
            stack.push_async_callback(queue.disconnect)

        llm: AsyncOpenAI | None = None
        if settings.llm_enabled:
            llm = AsyncOpenAI(
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key.get_secret_value(),
                timeout=settings.llm_timeout_seconds,
                max_retries=1,
            )
            stack.push_async_callback(llm.close)
        else:
            log.warning("llm.disabled", reason="thiếu LLM_BASE_URL hoặc LLM_MODEL")

        http = CurlSession(impersonate="chrome")
        stack.push_async_callback(http.close)

        browser = BrowserManager(max_pages=settings.browser_max_pages)
        stack.push_async_callback(browser.close)

        yield Resources(
            settings=settings,
            engine=engine,
            sessionmaker=create_sessionmaker(engine),
            redis=redis,
            queue=queue,
            llm=llm,
            http=http,
            browser=browser,
        )
