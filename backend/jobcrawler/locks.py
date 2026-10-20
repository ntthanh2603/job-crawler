"""Khoá phân tán có hạn (lease) trên Redis chính: một process giữ, các process khác chờ / bỏ qua.

`SET key token NX PX ttl` để giữ; gia hạn / nhả bằng Lua chỉ khi giá trị còn là token của mình
(process chậm không nhả nhầm khoá người khác đã lấy sau khi khoá của nó hết hạn). Process chết thì
khoá tự hết hạn sau `ttl`. Không phụ thuộc phiên Postgres nên đúng cả khi Postgres đứng sau
PgBouncer chế độ transaction (khoá advisory theo phiên không dùng được ở đó).

`hold()` tự gia hạn mỗi ttl/3; mất khoá (người khác đã lấy, hoặc không gia hạn được suốt `ttl`) thì
huỷ khối đang chạy và ném `LeaseLost` — process đứng lâu hơn `ttl` (GC, máy ngủ) rồi chạy tiếp tự
dừng ở lần gia hạn kế tiếp. Khoảng chồng ngắn đó vẫn có thể xảy ra: việc ghi DB cần an toàn tuyệt
đối thì kèm fencing token (`db/fencing.py`, khoá theo doanh nghiệp `services/crawl.company_lock`).

Dùng cho: leader của scheduler (`entrypoints/scheduler.py`, gọi `acquire()` mỗi nhịp, chỉ xếp job
có khoá duy nhất nên chạy trùng vô hại), lượt bảo trì (`services/maintenance.py`), khoá theo doanh
nghiệp (`services/crawl.py`).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from jobcrawler.logging import get_logger

log = get_logger(__name__)

ACQUIRE_POLL_SECONDS = 0.5

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


class LeaseLost(Exception):
    """Mất khoá giữa lúc giữ (`RedisLease.hold()`): khối đang chạy đã bị huỷ."""


def owner_token() -> str:
    """Token của người giữ: máy + pid + ngẫu nhiên (đọc được khi xem khoá trên Redis)."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


class RedisLease:
    def __init__(self, redis: Any, key: str, *, ttl: float, token: str | None = None) -> None:
        self.redis = redis
        self.key = key
        self.ttl_ms = max(1000, int(ttl * 1000))
        self.token = token or owner_token()

    async def acquire(self, *, wait: float = 0) -> bool:
        """Giữ khoá nếu đang trống (hoặc mình đang giữ: gia hạn); `wait` > 0: thử lại mỗi
        ACQUIRE_POLL_SECONDS tới chừng đó giây."""
        deadline = time.monotonic() + max(0.0, wait)
        while True:
            if await self.redis.set(self.key, self.token, nx=True, px=self.ttl_ms):
                return True
            if await self.renew():
                return True
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            await asyncio.sleep(min(ACQUIRE_POLL_SECONDS, left))

    async def renew(self) -> bool:
        """Gia hạn nếu mình còn giữ; False = đã mất khoá (hết hạn, người khác lấy)."""
        result = await self.redis.eval(_RENEW, 1, self.key, self.token, self.ttl_ms)
        return bool(result)

    async def release(self) -> None:
        await self.redis.eval(_RELEASE, 1, self.key, self.token)

    @contextlib.asynccontextmanager
    async def hold(self, *, wait: float = 0) -> AsyncIterator[bool]:
        """`async with lease.hold() as held`: giữ khoá (chờ tối đa `wait` giây) và tự gia hạn mỗi
        ttl/3 tới khi ra khỏi khối; `held` False = process khác đang giữ. Mất khoá giữa chừng thì
        huỷ khối và ném `LeaseLost`."""
        if not await self.acquire(wait=wait):
            yield False
            return
        body = asyncio.current_task()
        lost = False

        async def keep() -> None:
            nonlocal lost
            ttl = self.ttl_ms / 1000
            renewed = time.monotonic()
            while True:
                await asyncio.sleep(ttl / 3)
                try:
                    if await self.renew():
                        renewed = time.monotonic()
                        continue
                    log.warning("lock.lost", key=self.key)
                except Exception as exc:  # Redis chập chờn: thử lại ở nhịp sau, tới khi quá ttl
                    if time.monotonic() - renewed < ttl:
                        log.warning("lock.renew_failed", key=self.key, error=str(exc))
                        continue
                    log.warning("lock.lost", key=self.key, error=str(exc))
                lost = True
                if body is not None:
                    body.cancel()
                return

        keeper = asyncio.create_task(keep(), name=f"lease:{self.key}")
        try:
            yield True
        except BaseException as exc:
            # Gỡ lần huỷ của `keep`; còn lần huỷ từ ngoài thì để CancelledError đi tiếp. Khối kết
            # thúc bằng lỗi khác (vd. `FencedOut` khi dọn dẹp) thì giữ lỗi đó.
            if not lost or body is None or not body.cancelling():
                raise
            if body.uncancel() > 0 or not isinstance(exc, asyncio.CancelledError):
                raise
            raise LeaseLost(f"mất khoá {self.key}") from None
        else:
            if lost:  # khối đã nuốt lần huỷ rồi chạy xong: vẫn báo mất khoá
                if body is not None and body.cancelling():
                    body.uncancel()
                raise LeaseLost(f"mất khoá {self.key}")
        finally:
            keeper.cancel()
            # `wait` (không `await keeper`): huỷ từ ngoài khối lúc này vẫn được truyền tiếp.
            await asyncio.wait({keeper})
            if not lost:
                with contextlib.suppress(Exception):
                    await self.release()
