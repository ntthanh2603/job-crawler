"""Rate limit theo domain bằng token bucket.

`RedisRateLimiter` chạy toàn bộ phép tính trong một Lua script (dùng đồng hồ của Redis) nên
mọi process và worker dùng chung một bucket cho mỗi domain. `penalize()` tạm dừng cả domain,
ví dụ sau khi nhận 429 kèm `Retry-After`.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from redis.asyncio import Redis

Sleep = Callable[[float], Awaitable[None]]


class RateLimiter(Protocol):
    async def acquire(self, domain: str, *, rate: float, burst: int = 1) -> float:
        """Chờ tới khi lấy được một token. Trả về tổng thời gian đã chờ (giây)."""
        ...

    async def penalize(self, domain: str, seconds: float) -> None:
        """Không cấp token cho `domain` trong `seconds` giây."""
        ...


# Trả về số ms phải chờ (0 = đã lấy được token).
_TOKEN_BUCKET_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local paused = redis.call('PTTL', KEYS[2])
if paused > 0 then
  return paused
end
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local data = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil or ts == nil then
  tokens = burst
  ts = now
end
tokens = math.min(burst, tokens + math.max(0, now - ts) * rate / 1000)
local wait = 0
if tokens >= 1 then
  tokens = tokens - 1
else
  wait = math.ceil((1 - tokens) * 1000 / rate)
end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('PEXPIRE', KEYS[1], math.ceil(burst * 1000 / rate) + 1000)
return wait
"""


class RedisRateLimiter:
    def __init__(self, redis: Redis, *, prefix: str = "jc", sleep: Sleep = asyncio.sleep) -> None:
        self._redis = redis
        self._prefix = prefix
        self._sleep = sleep
        self._script: Any = redis.register_script(_TOKEN_BUCKET_LUA)

    async def acquire(self, domain: str, *, rate: float, burst: int = 1) -> float:
        keys = [f"{self._prefix}:rl:{domain}", f"{self._prefix}:rl:pause:{domain}"]
        waited = 0.0
        while True:
            wait_ms = int(await self._script(keys=keys, args=[rate, burst]))
            if wait_ms <= 0:
                return waited
            # Jitter nhỏ để các process không cùng thức dậy một lúc.
            delay = wait_ms / 1000 + random.uniform(0, 0.05)
            await self._sleep(delay)
            waited += delay

    async def penalize(self, domain: str, seconds: float) -> None:
        if seconds <= 0:
            return
        r: Any = self._redis
        key = f"{self._prefix}:rl:pause:{domain}"
        # Chỉ kéo dài, không rút ngắn lần tạm dừng đang có.
        current = int(await r.pttl(key))
        ms = int(seconds * 1000)
        if ms > current:
            await r.set(key, b"1", px=ms)


class MemoryRateLimiter:
    """Cùng thuật toán, trong bộ nhớ của một process (test, CLI một process)."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._buckets: dict[str, tuple[float, float]] = {}
        self._paused_until: dict[str, float] = {}
        self.calls: list[tuple[str, float, int]] = []

    def _try(self, domain: str, rate: float, burst: int) -> float:
        now = self._clock()
        paused = self._paused_until.get(domain, 0.0) - now
        if paused > 0:
            return paused
        tokens, ts = self._buckets.get(domain, (float(burst), now))
        tokens = min(float(burst), tokens + max(0.0, now - ts) * rate)
        if tokens >= 1:
            self._buckets[domain] = (tokens - 1, now)
            return 0.0
        self._buckets[domain] = (tokens, now)
        return (1 - tokens) / rate

    async def acquire(self, domain: str, *, rate: float, burst: int = 1) -> float:
        self.calls.append((domain, rate, burst))
        waited = 0.0
        while (wait := self._try(domain, rate, burst)) > 0:
            await self._sleep(wait)
            waited += wait
        return waited

    async def penalize(self, domain: str, seconds: float) -> None:
        until = self._clock() + seconds
        self._paused_until[domain] = max(self._paused_until.get(domain, 0.0), until)
