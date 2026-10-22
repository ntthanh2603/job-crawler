"""Hàng chờ có ưu tiên của cả cụm (`RedisSemaphore`) trên Redis thật: giới hạn số chỗ, thứ tự ưu
tiên, chỗ / người chờ của process chết tự gỡ, co giãn số chỗ."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from jobcrawler.common.config import Settings
from jobcrawler.services.redis import semaphore
from jobcrawler.services.redis.semaphore import RedisSemaphore

from .conftest import _env

pytestmark = pytest.mark.integration


@pytest.fixture
async def redis() -> AsyncIterator[tuple[Redis, str]]:
    client = Redis.from_url(_env("REDIS_URL") or Settings().redis_url)
    try:
        await client.ping()  # pyright: ignore[reportGeneralTypeIssues]
    except Exception:
        await client.aclose()
        pytest.skip("không kết nối được Redis")
    prefix = f"jctest:{uuid4().hex[:8]}"
    yield client, prefix
    # Xoá đúng khoá của semaphore: quét cả keyspace của Redis dev đầy dữ liệu mất hàng chục giây.
    await client.delete(*(f"{prefix}:sem:llm:{k}" for k in ("holders", "waiters", "seen", "cap")))
    await client.delete(f"{prefix}:sem:llm:calm", f"{prefix}:sem:llm:cooldown")
    await client.aclose()


def _sem(redis: tuple[Redis, str], limit: int, lease: float = 30) -> RedisSemaphore:
    client, prefix = redis
    return RedisSemaphore(client, "llm", limit=limit, lease=lease, prefix=prefix)


async def test_limit_is_shared_between_processes(redis: tuple[Redis, str]) -> None:
    a, b = _sem(redis, 2), _sem(redis, 2)  # như hai process
    one = await a.acquire(5, timeout=1)
    two = await b.acquire(5, timeout=1)
    assert one
    assert two
    assert await a.acquire(5, timeout=0.3) is None  # hết chỗ: chờ rồi bỏ, đã rời hàng
    assert await a.state() == {"used": 2, "waiting": 0, "capacity": 2}
    await b.release(two)
    assert await a.acquire(5, timeout=1)


async def test_lower_priority_number_goes_first(redis: tuple[Redis, str]) -> None:
    sem = _sem(redis, 1)
    holder = await sem.acquire(5, timeout=1)
    assert holder
    order: list[str] = []

    async def wait(name: str, priority: int) -> None:
        token = await sem.acquire(priority, timeout=5)
        assert token
        order.append(name)
        await sem.release(token)

    late = asyncio.create_task(wait("thấp", 9))
    await asyncio.sleep(0.1)
    urgent = asyncio.create_task(wait("cao", 1))  # đến sau nhưng ưu tiên hơn
    await asyncio.sleep(0.1)
    await sem.release(holder)
    await asyncio.gather(late, urgent)
    assert order == ["cao", "thấp"]


async def test_cancelled_waiter_leaves_the_queue(redis: tuple[Redis, str]) -> None:
    sem = _sem(redis, 1)
    assert await sem.acquire(5, timeout=1)
    waiter = asyncio.create_task(sem.acquire(5, timeout=10))
    await asyncio.sleep(0.1)
    assert (await sem.state())["waiting"] == 1
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert (await sem.state())["waiting"] == 0


async def test_dead_process_does_not_block(
    redis: tuple[Redis, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(semaphore, "STALE_MS", 200)
    sem = _sem(redis, 1, lease=0.3)
    assert await sem.acquire(5, timeout=1)  # chủ chỗ chết, không trả: hết `lease` thì tự trả
    # Người chờ ưu tiên cao của process chết: hỏi một lần rồi im.
    dead = await sem._acquire(  # pyright: ignore[reportPrivateUsage]
        keys=sem.keys, args=["dead", 0, sem.lease_ms, sem.limit, semaphore.STALE_MS]
    )
    assert dead == 0
    assert await sem.acquire(5, timeout=2)


async def test_shrink_and_grow_capacity(
    redis: tuple[Redis, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    sem = _sem(redis, 3)
    assert await sem.shrink() == 2
    assert await sem.shrink() == 2  # nhiều 429 cùng lúc chỉ bớt một chỗ
    monkeypatch.setattr(semaphore, "SHRINK_COOLDOWN_MS", 50)
    await asyncio.sleep(1.05)
    assert await sem.shrink() == 1
    await asyncio.sleep(0.06)
    assert await sem.shrink() == 1  # tối thiểu một chỗ
    assert await sem.acquire(5, timeout=1)
    assert await sem.acquire(5, timeout=0.3) is None
    for _ in range(3):
        assert await sem.grow() == 1
    assert await sem.grow() == 2  # 1 × 4 lời gọi thành công
    assert await sem.acquire(5, timeout=1)
    for _ in range(8):
        await sem.grow()
    assert await sem.grow() == 3  # không quá `limit`
