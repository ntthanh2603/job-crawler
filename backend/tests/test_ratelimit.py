from __future__ import annotations

import pytest

from jobcrawler.services.net.ratelimit import MemoryRateLimiter

from .conftest import FakeClock


@pytest.fixture
def limiter(clock: FakeClock) -> MemoryRateLimiter:
    return MemoryRateLimiter(clock=clock, sleep=clock.sleep)


async def test_one_per_second(limiter: MemoryRateLimiter, clock: FakeClock) -> None:
    assert await limiter.acquire("a.vn", rate=1) == 0
    assert await limiter.acquire("a.vn", rate=1) == pytest.approx(1.0)
    assert await limiter.acquire("a.vn", rate=2) == pytest.approx(0.5)
    assert sum(clock.sleeps) == pytest.approx(1.5)


async def test_burst_then_wait(limiter: MemoryRateLimiter) -> None:
    waits = [await limiter.acquire("a.vn", rate=1, burst=3) for _ in range(4)]
    assert waits[:3] == [0, 0, 0]
    assert waits[3] == pytest.approx(1.0)


async def test_tokens_refill_over_time(limiter: MemoryRateLimiter, clock: FakeClock) -> None:
    await limiter.acquire("a.vn", rate=1)
    clock.now += 5
    assert await limiter.acquire("a.vn", rate=1) == 0


async def test_domains_are_independent(limiter: MemoryRateLimiter) -> None:
    await limiter.acquire("a.vn", rate=1)
    assert await limiter.acquire("b.vn", rate=1) == 0


async def test_penalize_pauses_domain(limiter: MemoryRateLimiter) -> None:
    await limiter.penalize("a.vn", 10)
    await limiter.penalize("a.vn", 3)  # không rút ngắn lần tạm dừng đang có
    assert await limiter.acquire("a.vn", rate=1) == pytest.approx(10)
    assert await limiter.acquire("b.vn", rate=1) == 0
