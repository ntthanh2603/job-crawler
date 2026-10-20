"""Khoá phân tán có hạn (lease) trên Redis chính: một process giữ, các process khác chờ / bỏ qua.

`SET key token NX PX ttl` để giữ; gia hạn / nhả bằng Lua chỉ khi giá trị còn là token của mình
(process chậm không nhả nhầm khoá người khác đã lấy sau khi khoá của nó hết hạn). Process chết thì
khoá tự hết hạn sau `ttl`. Không phụ thuộc phiên Postgres nên đúng cả khi Postgres đứng sau
PgBouncer chế độ transaction (khoá advisory theo phiên không dùng được ở đó).

Dùng cho: leader của scheduler (`entrypoints/scheduler.py`), lượt bảo trì
(`services/maintenance.py`). Lease không có fencing token: process đứng lâu hơn `ttl` (GC, máy
ngủ) rồi chạy tiếp có thể chồng với process mới giữ khoá trong chốc lát — việc được bảo vệ phải chịu
được chạy trùng (scheduler chỉ xếp job có khoá duy nhất).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import uuid
from collections.abc import AsyncIterator
from typing import Any

from jobcrawler.logging import get_logger

log = get_logger(__name__)

_RENEW = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


def owner_token() -> str:
    """Token của người giữ: máy + pid + ngẫu nhiên (đọc được khi xem khoá trên Redis)."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class RedisLease:
    def __init__(self, redis: Any, key: str, *, ttl: float, token: str | None = None) -> None:
        self.redis = redis
        self.key = key
        self.ttl_ms = max(1000, int(ttl * 1000))
        self.token = token or owner_token()

    async def acquire(self) -> bool:
        """Giữ khoá nếu đang trống (hoặc mình đang giữ: gia hạn). Không chờ."""
        if await self.redis.set(self.key, self.token, nx=True, px=self.ttl_ms):
            return True
        return await self.renew()

    async def renew(self) -> bool:
        """Gia hạn nếu mình còn giữ; False = đã mất khoá (hết hạn, người khác lấy)."""
        result = await self.redis.eval(_RENEW, 1, self.key, self.token, self.ttl_ms)
        return bool(result)

    async def release(self) -> None:
        await self.redis.eval(_RELEASE, 1, self.key, self.token)

    @contextlib.asynccontextmanager
    async def hold(self) -> AsyncIterator[bool]:
        """`async with lease.hold() as held`: giữ khoá (không chờ) và tự gia hạn mỗi ttl/3 tới khi
        ra khỏi khối; `held` False = process khác đang giữ. Mất khoá giữa chừng thì ghi log."""
        if not await self.acquire():
            yield False
            return

        async def keep() -> None:
            while True:
                await asyncio.sleep(self.ttl_ms / 3000)
                try:
                    if not await self.renew():
                        log.warning("lock.lost", key=self.key)
                        return
                except Exception as exc:  # Redis chập chờn: thử lại ở nhịp sau
                    log.warning("lock.renew_failed", key=self.key, error=str(exc))

        task = asyncio.create_task(keep(), name=f"lease:{self.key}")
        try:
            yield True
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            with contextlib.suppress(Exception):
                await self.release()
