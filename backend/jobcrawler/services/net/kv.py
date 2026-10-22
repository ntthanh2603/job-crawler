"""Kho key-value tối giản cho trạng thái dùng chung giữa các process.

`RedisKV` dùng khi chạy thật; `MemoryKV` dùng trong test (và khi chỉ có một process).
Proxy pool, robots cache và HTTP cache chỉ phụ thuộc vào giao diện `KeyValueStore`.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from redis.asyncio import Redis

Value = bytes | str | int | float


def _to_bytes(value: Value) -> bytes:
    if isinstance(value, bytes):
        return value
    return str(value).encode()


class KeyValueStore(Protocol):
    async def get(self, key: str) -> bytes | None: ...
    async def mget(self, keys: Sequence[str]) -> list[bytes | None]: ...
    async def set(self, key: str, value: Value, *, ttl: float | None = None) -> None: ...
    async def mset(self, items: Mapping[str, Value], *, ttl: float | None = None) -> None: ...
    async def set_nx(self, key: str, value: Value, *, ttl: float | None = None) -> bool: ...
    async def delete(self, *keys: str) -> None: ...
    async def incrby(self, key: str, amount: int = 1) -> int: ...


class RedisKV:
    def __init__(self, redis: Redis, *, prefix: str = "jc") -> None:
        self._redis = redis
        self._prefix = prefix

    def _k(self, key: str) -> str:
        return f"{self._prefix}:{key}"

    async def get(self, key: str) -> bytes | None:
        r: Any = self._redis
        return await r.get(self._k(key))

    async def mget(self, keys: Sequence[str]) -> list[bytes | None]:
        if not keys:
            return []
        r: Any = self._redis
        return list(await r.mget([self._k(k) for k in keys]))

    async def set(self, key: str, value: Value, *, ttl: float | None = None) -> None:
        r: Any = self._redis
        px = max(1, int(ttl * 1000)) if ttl else None
        await r.set(self._k(key), _to_bytes(value), px=px)

    async def mset(self, items: Mapping[str, Value], *, ttl: float | None = None) -> None:
        """Nhiều khoá trong một lượt gửi (pipeline, không transaction)."""
        if not items:
            return
        r: Any = self._redis
        px = max(1, int(ttl * 1000)) if ttl else None
        async with r.pipeline(transaction=False) as pipe:
            for key, value in items.items():
                pipe.set(self._k(key), _to_bytes(value), px=px)
            await pipe.execute()

    async def set_nx(self, key: str, value: Value, *, ttl: float | None = None) -> bool:
        r: Any = self._redis
        px = max(1, int(ttl * 1000)) if ttl else None
        return bool(await r.set(self._k(key), _to_bytes(value), px=px, nx=True))

    async def delete(self, *keys: str) -> None:
        if keys:
            r: Any = self._redis
            await r.delete(*(self._k(k) for k in keys))

    async def incrby(self, key: str, amount: int = 1) -> int:
        r: Any = self._redis
        return int(await r.incrby(self._k(key), amount))


class MemoryKV:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._data: dict[str, tuple[bytes, float | None]] = {}

    def _live(self, key: str) -> bytes | None:
        item = self._data.get(key)
        if item is None:
            return None
        value, expires = item
        if expires is not None and expires <= self._clock():
            del self._data[key]
            return None
        return value

    def _expiry(self, ttl: float | None) -> float | None:
        return self._clock() + ttl if ttl else None

    async def get(self, key: str) -> bytes | None:
        return self._live(key)

    async def mget(self, keys: Sequence[str]) -> list[bytes | None]:
        return [self._live(k) for k in keys]

    async def set(self, key: str, value: Value, *, ttl: float | None = None) -> None:
        self._data[key] = (_to_bytes(value), self._expiry(ttl))

    async def mset(self, items: Mapping[str, Value], *, ttl: float | None = None) -> None:
        for key, value in items.items():
            self._data[key] = (_to_bytes(value), self._expiry(ttl))

    async def set_nx(self, key: str, value: Value, *, ttl: float | None = None) -> bool:
        if self._live(key) is not None:
            return False
        self._data[key] = (_to_bytes(value), self._expiry(ttl))
        return True

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self._data.pop(key, None)

    async def incrby(self, key: str, amount: int = 1) -> int:
        current = self._live(key)
        value = int(current or 0) + amount
        expires = self._data[key][1] if current is not None else None
        self._data[key] = (str(value).encode(), expires)
        return value
