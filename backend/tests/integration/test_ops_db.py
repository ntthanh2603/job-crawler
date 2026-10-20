"""Vận hành trên Postgres + Redis thật: heartbeat health của scheduler / runner, tách Redis cache
khỏi Redis chính."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from jobcrawler import heartbeat
from jobcrawler.config import Settings
from jobcrawler.entrypoints import runner, scheduler
from jobcrawler.net.http import FetchResult
from jobcrawler.queues import drop_queue
from jobcrawler.resources import open_resources
from jobcrawler.schemas import FetchOutcome
from jobcrawler.services import crawl, overview, schedule, sla

pytestmark = pytest.mark.integration


def tagged(base: Settings, **update: Any) -> Settings:
    tag = uuid4().hex[:8]
    return base.model_copy(
        update={
            "redis_prefix": f"jc-test-{tag}",
            "build_queue_name": f"jc-test-build-{tag}",
            "poll_queue_name": f"jc-test-poll-{tag}",
            "board_queue_name": f"jc-test-boards-{tag}",
            "llm_base_url": None,
            "serp_api_key": None,
            **update,
        }
    )


async def keys_of(redis: Redis, pattern: str) -> set[str]:
    return {k.decode() async for k in redis.scan_iter(match=pattern, count=1000)}


async def cleanup(settings: Settings, *urls: str) -> None:
    for name in settings.queue_names:
        await drop_queue(settings.redis_url, name)
    for url in {settings.redis_url, *urls}:
        redis = Redis.from_url(url)
        try:
            keys = await keys_of(redis, f"{settings.redis_prefix}:*")
            if keys:
                await redis.delete(*keys)
        finally:
            await redis.aclose()


async def wait_until(check: Callable[[], Awaitable[bool]], seconds: float = 15) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if await check():
            return True
        await asyncio.sleep(0.2)
    return False


@pytest.fixture
def no_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nhịp scheduler không xếp lịch thật (DB test dùng chung với test khác)."""

    async def tick(res: Any, now: Any = None) -> dict[str, int]:
        return {}

    monkeypatch.setattr(schedule, "tick", tick)


# --- heartbeat health --------------------------------------------------------------------------


@pytest.mark.usefixtures("no_tick")
async def test_scheduler_loop_writes_heartbeat(integration_settings: Settings) -> None:
    settings = tagged(integration_settings)
    try:
        async with open_resources(settings, db_logs=False, live_settings=False) as res:
            stop = asyncio.Event()
            task = asyncio.create_task(scheduler.run_loop(res, stop))
            try:

                async def healthy() -> bool:
                    ok, _ = await heartbeat.check(res.redis, settings.redis_prefix, "scheduler")
                    return ok

                assert await wait_until(healthy)
                key = heartbeat.heartbeat_key(settings.redis_prefix, "scheduler")
                ttl = await res.redis.ttl(key)
                assert 0 < ttl <= heartbeat.HEARTBEAT_TTL
            finally:
                stop.set()
                await asyncio.wait_for(task, 15)
            ok, reason = await heartbeat.check(
                res.redis, settings.redis_prefix, "runner-poll", max_age=60
            )
            assert not ok
            assert "không có heartbeat" in reason
    finally:
        await cleanup(settings)


async def test_runner_role_writes_its_component_heartbeat(integration_settings: Settings) -> None:
    settings = tagged(integration_settings)
    probe = Redis.from_url(settings.redis_url)
    task = asyncio.create_task(runner.run_runner(settings, ["poll"]))
    try:

        async def healthy() -> bool:
            if task.done():
                task.result()  # runner chết: ném lỗi của nó
            ok, _ = await heartbeat.check(probe, settings.redis_prefix, "runner-poll")
            return ok

        assert await wait_until(healthy, 30)
        ok, _ = await heartbeat.check(probe, settings.redis_prefix, "runner", max_age=60)
        assert not ok  # một vai: component theo vai, không phải `runner`
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, 30)
        await probe.aclose()
        await cleanup(settings)


# --- tách Redis cache --------------------------------------------------------------------------


def other_db(url: str, db: int) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{db}"))


async def test_cache_keys_go_to_cache_redis(integration_settings: Settings) -> None:
    main_db = int(urlsplit(integration_settings.redis_url).path.strip("/") or 0)
    cache_url = other_db(integration_settings.redis_url, 15 if main_db != 15 else 14)
    settings = tagged(integration_settings, cache_redis_url=cache_url)
    prefix = settings.redis_prefix
    try:
        async with open_resources(settings, db_logs=False, live_settings=False) as res:
            assert res.cache_redis is not res.redis
            page = FetchResult(
                url="https://acme.vn/jobs",
                final_url="https://acme.vn/jobs",
                status=200,
                outcome=FetchOutcome.OK,
                content=b"<html>ok</html>",
            )
            await res.http._cache.set(page)  # pyright: ignore[reportPrivateUsage]
            assert await res.api_http._cache.get(page.url) is not None  # pyright: ignore[reportPrivateUsage]
            await res.cache_kv.set("discovery:acme.vn", "{}", ttl=60)
            await overview.refresh_charts(res)
            await res.queues.poll.enqueue("poll_company", key="split", company_id="x")
            await sla.record_poll(res.redis, prefix, status="ok", duration_s=1, window_s=60)
            async with crawl.company_lock(res, uuid4(), ttl=30) as held:
                assert held
                main = await keys_of(res.redis, f"{prefix}:*")
            cache = await keys_of(res.cache_redis, f"{prefix}:*")
            queue_main = await keys_of(res.redis, f"sq:{settings.poll_queue_name}:*")
            queue_cache = await keys_of(res.cache_redis, f"sq:{settings.poll_queue_name}:*")

        def has(keys: set[str], part: str) -> bool:
            return any(k.startswith(f"{prefix}:{part}") for k in keys)

        for part in ("http:", "discovery:", "overview:charts"):
            assert has(cache, part), part
            assert not has(main, part), part
        for part in ("sla:poll:", "lock:company:"):
            assert has(main, part), part
            assert not has(cache, part), part
        assert queue_main
        assert not queue_cache
    finally:
        await cleanup(settings, cache_url)


# --- khoá phân tán (lease Redis) ------------------------------------------------------------------


async def test_redis_lease_one_holder_renew_release_expire(integration_settings: Settings) -> None:
    from jobcrawler.locks import RedisLease

    settings = tagged(integration_settings)
    redis = Redis.from_url(settings.redis_url)
    key = f"{settings.redis_prefix}:lock:test"
    try:
        a = RedisLease(redis, key, ttl=1)
        b = RedisLease(redis, key, ttl=1)
        assert await a.acquire()
        assert await a.acquire()  # mình đang giữ: gia hạn
        assert not await b.acquire()
        assert not await b.renew()
        await b.release()  # không nhả khoá của người khác
        assert (await redis.get(key)).decode() == a.token
        await a.release()
        assert await b.acquire()
        await asyncio.sleep(1.2)  # b không gia hạn: hết hạn, a lấy được
        assert await a.acquire()
        assert not await b.renew()
        async with b.hold() as held:
            assert not held
        await a.release()
        async with b.hold() as held:
            assert held
            await asyncio.sleep(1.5)  # tự gia hạn trong lúc giữ
            assert (await redis.get(key)).decode() == b.token
        assert await redis.get(key) is None  # ra khỏi khối: nhả
    finally:
        await redis.delete(key)
        await redis.aclose()


async def test_redis_lease_lost_cancels_the_body(integration_settings: Settings) -> None:
    from jobcrawler.locks import LeaseLost, RedisLease

    settings = tagged(integration_settings)
    redis = Redis.from_url(settings.redis_url)
    key = f"{settings.redis_prefix}:lock:test-lost"
    try:
        lease = RedisLease(redis, key, ttl=1)
        cleaned: list[bool] = []

        async def work() -> None:
            async with lease.hold() as held:
                assert held
                await redis.set(key, "process-khac")  # khoá đã hết hạn, process khác lấy
                try:
                    await asyncio.sleep(10)
                finally:
                    cleaned.append(True)

        t0 = time.monotonic()
        with pytest.raises(LeaseLost):
            await work()
        assert time.monotonic() - t0 < 2  # huỷ ở lần gia hạn kế tiếp (ttl/3)
        assert cleaned == [True]
        assert await redis.get(key) == b"process-khac"  # không nhả khoá của người khác
        # Huỷ từ ngoài vẫn là CancelledError, không bị đổi thành LeaseLost.
        other = RedisLease(redis, f"{key}:2", ttl=5)

        async def hold_forever() -> None:
            async with other.hold():
                await asyncio.sleep(10)

        job = asyncio.create_task(hold_forever())
        await asyncio.sleep(0.1)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        assert await redis.get(f"{key}:2") is None
    finally:
        await redis.delete(key, f"{key}:2")
        await redis.aclose()


async def test_only_one_scheduler_leads(
    integration_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticks: list[str] = []

    async def tick(res: Any, now: Any = None) -> dict[str, int]:
        ticks.append(asyncio.current_task().get_name())  # pyright: ignore[reportOptionalMemberAccess]
        return {}

    monkeypatch.setattr(schedule, "tick", tick)
    monkeypatch.setattr(scheduler, "STANDBY_SECONDS", 0.2)
    settings = tagged(integration_settings, scheduler_tick_seconds=0.1)
    try:
        async with open_resources(settings, db_logs=False, live_settings=False) as res:
            stop_a, stop_b = asyncio.Event(), asyncio.Event()
            a = asyncio.create_task(scheduler.run_loop(res, stop_a), name="a")
            await asyncio.sleep(0.3)
            b = asyncio.create_task(scheduler.run_loop(res, stop_b), name="b")
            await asyncio.sleep(0.8)
            assert set(ticks) == {"a"}  # b chờ: a đang giữ khoá leader
            stop_a.set()
            await asyncio.wait_for(a, 5)  # a dừng: nhả khoá, b thay ngay
            count = len(ticks)
            assert await wait_until(
                lambda: asyncio.sleep(0, len(ticks) > count and ticks[-1] == "b"), 5
            )
            stop_b.set()
            await asyncio.wait_for(b, 5)
            assert await res.redis.get(scheduler.leader_key(settings.redis_prefix)) is None
    finally:
        await cleanup(settings)
