"""Cài đặt hiện hành của một process: env/.env + các giá trị sửa ở trang Cài đặt (`app_settings`).

Lưu ở trang Cài đặt tăng phiên bản trên Redis (`<prefix>:settings:version`); mỗi process kiểm tra
phiên bản mỗi `interval` giây và nạp lại khi đổi. Giá trị đọc qua `res.settings` lúc dùng (lịch
poll, giới hạn nguồn, ngưỡng xác minh...) có hiệu lực ngay; giá trị dùng lúc dựng tài nguyên
(endpoint LLM, cấu hình HTTP, cron) cần khởi động lại process — trang Cài đặt ghi rõ từng mục.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobcrawler.common.config import Settings
from jobcrawler.common.logging import get_logger
from jobcrawler.modules.settings import app_settings

log = get_logger(__name__)


class LiveSettings:
    def __init__(
        self,
        base: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        redis: Redis,
        *,
        interval: float = 5.0,
        on_change: Callable[[Settings], None] | None = None,
    ) -> None:
        self.base = base
        self.current = base
        self.version = -1
        self.on_change = on_change
        self._sessionmaker = sessionmaker
        self._redis = redis
        self._interval = interval
        self._task: asyncio.Task[None] | None = None

    async def load(self) -> Settings:
        """Nạp lần đầu (lúc khởi động process)."""
        await self.refresh(force=True)
        return self.current

    async def refresh(self, *, force: bool = False) -> bool:
        """Nạp lại nếu phiên bản đổi. Trả True khi cài đặt hiện hành đổi."""
        version = await app_settings.settings_version(self._redis, self.base.redis_prefix)
        if not force and version == self.version:
            return False
        settings = await app_settings.load_effective(self._sessionmaker, self.base)
        self.version = version
        changed = settings != self.current
        self.current = settings
        if changed and not force:
            log.info("settings.reloaded", version=version)
            if self.on_change is not None:
                self.on_change(settings)
        return changed

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.refresh()
            except Exception:  # DB / Redis tạm lỗi: giữ cài đặt đang dùng
                log.warning("settings.reload_failed", exc_info=True)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
