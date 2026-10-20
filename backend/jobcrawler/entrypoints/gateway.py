"""`jobcrawler gateway`: gateway gRPC cho worker lấy trang. Việc nằm trên Redis; Postgres chỉ để
đọc cài đặt sửa ở trang Cài đặt (cấu hình đẩy xuống worker) — không kết nối được thì dùng env.

Dừng bằng SIGTERM / Ctrl+C: báo NOT_SERVING, chờ lời gọi đang chạy tối đa STOP_GRACE_SECONDS.

Watchdog (`jobcrawler.watchdog`, WATCHDOG_SECONDS): event loop đứng quá ngưỡng thì process thoát
(mã 70) để Docker dựng lại.
"""

from __future__ import annotations

import asyncio
import os
import signal

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from jobcrawler import secretbox
from jobcrawler.config import Settings
from jobcrawler.db.session import create_engine, create_sessionmaker
from jobcrawler.gateway.server import STOP_GRACE_SECONDS, GatewayServer, SettingsProvider
from jobcrawler.logging import configure_logging, get_logger
from jobcrawler.resources import ResourceUnavailableError, redis_location
from jobcrawler.services.live_settings import LiveSettings
from jobcrawler.watchdog import start_watchdog

log = get_logger(__name__)

SIGNALS = (signal.SIGINT, signal.SIGTERM) if os.name != "nt" else ()


async def run_gateway(
    settings: Settings, *, settings_provider: SettingsProvider | None = None
) -> None:
    """Chạy gateway tới khi nhận tín hiệu dừng. `settings_provider`: cấu hình hiện hành đẩy xuống
    worker (mặc định `settings` lúc khởi động)."""
    configure_logging(settings)
    redis = Redis.from_url(settings.redis_url)
    try:
        try:
            await redis.ping()  # pyright: ignore[reportGeneralTypeIssues]
        except Exception as exc:
            where = redis_location(settings.redis_url)
            raise ResourceUnavailableError(f"Không kết nối được Redis ({where}): {exc}") from exc
        live = await _live_settings(settings, redis) if settings_provider is None else None
        if live is not None:
            settings_provider = lambda: live.current  # noqa: E731
        server = GatewayServer(redis, settings, settings_provider=settings_provider)
        await server.start()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in SIGNALS:
            loop.add_signal_handler(signum, stop.set)
        dog = start_watchdog("gateway", max_stall=settings.watchdog_seconds)
        try:
            stopping = asyncio.ensure_future(stop.wait())
            terminated = asyncio.ensure_future(server.wait())
            try:
                await asyncio.wait({stopping, terminated}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                stopping.cancel()
                terminated.cancel()
        finally:
            dog.stop()  # trước lượt dừng: chờ lời gọi đang chạy không bị watchdog giết
            for signum in SIGNALS:
                loop.remove_signal_handler(signum)
            await server.stop(STOP_GRACE_SECONDS)
            if live is not None:
                await live.stop()
                await live.engine.dispose()
    finally:
        await redis.aclose()


class _GatewaySettings(LiveSettings):
    engine: AsyncEngine


async def _live_settings(settings: Settings, redis: Redis) -> _GatewaySettings | None:
    """Cài đặt hiện hành (env + trang Cài đặt), nạp lại khi đổi; None nếu không đọc được DB."""
    await secretbox.ensure(settings, redis)
    engine = create_engine(settings)
    live = _GatewaySettings(settings, create_sessionmaker(engine), redis)
    live.engine = engine
    try:
        await live.load()
    except Exception as exc:
        log.warning("gateway.settings_static", reason=f"không đọc được cài đặt từ DB: {exc}")
        await engine.dispose()
        return None
    live.start()
    return live
