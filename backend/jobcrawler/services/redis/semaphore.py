"""Semaphore có ưu tiên dùng chung mọi process trên Redis chính (`RedisSemaphore`): giới hạn số lời
gọi LLM đồng thời của CẢ CỤM. Endpoint LLM giới hạn số lời gọi đồng thời theo khoá (429
`key_concurrency_limit`), không theo process: nhiều process runner mỗi process tự giới hạn thì tổng
vượt giới hạn của khoá, lời gọi bị 429 rồi hết giờ chờ. Ở đây lời gọi vượt giới hạn xếp hàng chờ.

Khoá (`<REDIS_PREFIX>:sem:<tên>:…`):

- `holders` (ZSET token → hạn giữ, ms): chỗ đang dùng; process chết thì chỗ tự trả sau `lease`;
- `waiters` (ZSET token → hạng = ưu tiên × 10^13 + lúc xếp hàng, ms): hàng chờ — ưu tiên số nhỏ
  trước, cùng mức thì ai đến trước vào trước (như `llm.PrioritySemaphore`, nhưng cho cả cụm);
- `seen` (ZSET token → lần hỏi gần nhất, ms): người chờ hỏi lại mỗi `POLL_SECONDS`; process chết
  khi đang chờ thì bị gỡ khỏi hàng sau `STALE_MS`, không chặn người sau;
- `cap`: số chỗ hiện tại trong [1, `limit`] (giữ `CAP_TTL_SECONDS`). Endpoint báo quá số lời gọi
  đồng thời thì `shrink` bớt một chỗ cho cả cụm (nhiều 429 trong `SHRINK_COOLDOWN_MS` — các lời gọi
  đã vào trước khi bớt — chỉ bớt một, khoá `cooldown`); chạy trơn (`grow`: `cap` × 4 lời gọi thành
  công liên tiếp, đếm ở `calm`) thì thêm lại dần — tự dò ra giới hạn thật của khoá.

Mọi thao tác là một script Lua (nguyên tử), thời gian lấy từ `TIME` của Redis nên đồng hồ các máy
lệch nhau không làm sai hạn giữ.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from typing import Any, Protocol

from jobcrawler.common.logging import get_logger
from jobcrawler.services.redis.locks import owner_token

log = get_logger(__name__)

POLL_SECONDS = 0.25
STALE_MS = 5_000
SHRINK_COOLDOWN_MS = 1_000
CAP_TTL_SECONDS = 6 * 3600

_NOW = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
"""

_ACQUIRE = (
    _NOW
    + """
local holders, waiters, seen, capkey = KEYS[1], KEYS[2], KEYS[3], KEYS[4]
local token, priority = ARGV[1], tonumber(ARGV[2])
local lease, limit, stale = tonumber(ARGV[3]), tonumber(ARGV[4]), tonumber(ARGV[5])
redis.call('ZREMRANGEBYSCORE', holders, '-inf', now)
local dead = redis.call('ZRANGEBYSCORE', seen, '-inf', now - stale)
for _, t in ipairs(dead) do
  if t ~= token then
    redis.call('ZREM', waiters, t)
    redis.call('ZREM', seen, t)
  end
end
if redis.call('ZSCORE', holders, token) then
  return 1
end
-- 10^13 > mốc thời gian (ms): ưu tiên đứng trước lúc xếp hàng
redis.call('ZADD', waiters, 'NX', priority * 10000000000000 + now, token)
redis.call('ZADD', seen, now, token)
local cap = tonumber(redis.call('GET', capkey) or limit)
if cap > limit then cap = limit end
local free = cap - redis.call('ZCARD', holders)
if free > 0 and redis.call('ZRANK', waiters, token) < free then
  redis.call('ZREM', waiters, token)
  redis.call('ZREM', seen, token)
  redis.call('ZADD', holders, now + lease, token)
  return 1
end
return 0
"""
)

_RELEASE = """
redis.call('ZREM', KEYS[1], ARGV[1])
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('ZREM', KEYS[3], ARGV[1])
return 1
"""

_SHRINK = """
local cap = tonumber(redis.call('GET', KEYS[1]) or ARGV[1])
if cap > tonumber(ARGV[1]) then cap = tonumber(ARGV[1]) end
redis.call('DEL', KEYS[2])
-- nhiều 429 cùng lúc (lời gọi đã vào trước khi bớt chỗ) chỉ bớt một chỗ mỗi ARGV[3] ms
if cap > 1 and redis.call('SET', KEYS[3], 1, 'NX', 'PX', ARGV[3]) then
  cap = cap - 1
  redis.call('SET', KEYS[1], cap, 'EX', ARGV[2])
  return {cap, 1}
end
return {cap, 0}
"""

_GROW = """
local cap = tonumber(redis.call('GET', KEYS[1]) or ARGV[1])
if cap >= tonumber(ARGV[1]) then
  return cap
end
local calm = redis.call('INCR', KEYS[2])
redis.call('EXPIRE', KEYS[2], ARGV[2])
if calm >= cap * 4 then
  cap = cap + 1
  redis.call('SET', KEYS[1], cap, 'EX', ARGV[2])
  redis.call('DEL', KEYS[2])
end
return cap
"""


class Slots(Protocol):
    """Chỗ dùng chung có ưu tiên (`RedisSemaphore`; test dùng bản giả)."""

    async def acquire(self, priority: int, *, timeout: float) -> str | None: ...
    async def release(self, token: str) -> None: ...
    async def shrink(self) -> int: ...
    async def grow(self) -> int: ...


class RedisSemaphore:
    def __init__(
        self, redis: Any, name: str, *, limit: int, lease: float, prefix: str = "jc"
    ) -> None:
        self.redis = redis
        self.limit = max(1, limit)
        self.lease_ms = max(1, int(lease * 1000))
        base = f"{prefix}:sem:{name}"
        self.keys = [f"{base}:holders", f"{base}:waiters", f"{base}:seen", f"{base}:cap"]
        self._calm_key = f"{base}:calm"
        self._cooldown_key = f"{base}:cooldown"
        self._acquire: Any = redis.register_script(_ACQUIRE)
        self._release: Any = redis.register_script(_RELEASE)
        self._shrink: Any = redis.register_script(_SHRINK)
        self._grow: Any = redis.register_script(_GROW)

    async def acquire(self, priority: int, *, timeout: float) -> str | None:
        """Xếp hàng theo `priority` (số nhỏ trước) tới khi có chỗ; trả token để `release`, None nếu
        chờ quá `timeout` giây (đã rời hàng). Bị huỷ khi đang chờ thì cũng rời hàng."""
        token = owner_token()
        deadline = time.monotonic() + timeout
        args = [token, max(0, int(priority)), self.lease_ms, self.limit, STALE_MS]
        try:
            while True:
                if await self._acquire(keys=self.keys, args=args):
                    return token
                left = deadline - time.monotonic()
                if left <= 0:
                    await self.release(token)
                    return None
                await asyncio.sleep(min(left, POLL_SECONDS * random.uniform(0.8, 1.6)))
        except BaseException:
            with contextlib.suppress(Exception):
                await asyncio.shield(self.release(token))
            raise

    async def release(self, token: str) -> None:
        await self._release(keys=self.keys[:3], args=[token])

    async def shrink(self) -> int:
        """Endpoint báo quá số lời gọi đồng thời: bớt một chỗ cho cả cụm (tối thiểu 1)."""
        cap, shrunk = await self._shrink(
            keys=[self.keys[3], self._calm_key, self._cooldown_key],
            args=[self.limit, CAP_TTL_SECONDS, SHRINK_COOLDOWN_MS],
        )
        if shrunk:
            log.info("semaphore.shrink", key=self.keys[3], capacity=int(cap))
        return int(cap)

    async def grow(self) -> int:
        """Một lời gọi thành công: đủ `cap` × 4 lần liên tiếp thì thêm lại một chỗ (tới `limit`)."""
        return int(
            await self._grow(
                keys=[self.keys[3], self._calm_key], args=[self.limit, CAP_TTL_SECONDS]
            )
        )

    async def state(self) -> dict[str, int]:
        """Chỗ đang dùng / đang chờ / số chỗ hiện tại (để xem, không chính xác tuyệt đối)."""
        async with self.redis.pipeline(transaction=False) as pipe:
            pipe.zcard(self.keys[0])
            pipe.zcard(self.keys[1])
            pipe.get(self.keys[3])
            used, waiting, cap = await pipe.execute()
        return {"used": int(used), "waiting": int(waiting), "capacity": int(cap or self.limit)}
