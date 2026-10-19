"""Hàng đợi Redis Streams (`streamqueue`) trên Redis thật: khoá job, hẹn giờ, chia slot giữa các
worker, timeout / chạy lại, thu hồi job của worker chết, dừng êm, cron, chuyển job từ SAQ."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from jobcrawler.config import Settings
from jobcrawler.queues import drop_queue, move_jobs, queue_from_url
from jobcrawler.streamqueue import GROUP, CronJob, StreamQueue, StreamWorker

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def redis_url() -> str:
    url = Settings().redis_url

    async def ping() -> None:
        redis = Redis.from_url(url, socket_connect_timeout=3)
        try:
            await redis.ping()  # pyright: ignore[reportGeneralTypeIssues]
        finally:
            await redis.aclose()

    try:
        asyncio.run(ping())
    except Exception:
        pytest.skip(f"không kết nối được Redis {url}")
    return url


@pytest.fixture
async def queues(redis_url: str) -> AsyncIterator[Callable[..., Awaitable[StreamQueue]]]:
    """Tạo queue (đã connect) cùng một tên ngẫu nhiên; xoá sạch khi xong."""
    name = f"jobcrawler-test-{uuid4().hex[:8]}"
    opened: list[StreamQueue] = []

    async def make(reclaim_seconds: float = 60) -> StreamQueue:
        q = StreamQueue(redis_url, name, reclaim_seconds=reclaim_seconds)
        await q.connect()
        opened.append(q)
        return q

    try:
        yield make
    finally:
        for q in opened:
            await q.disconnect()
        await drop_queue(redis_url, name)


async def until(check: Callable[[], Awaitable[bool]], timeout: float = 20) -> float:
    started = time.monotonic()
    while not await check():
        assert time.monotonic() - started < timeout, "quá thời gian chờ"
        await asyncio.sleep(0.05)
    return time.monotonic() - started


async def stop(worker: StreamWorker, task: asyncio.Task[None]) -> None:
    worker.stop()
    await asyncio.wait_for(task, 20)


async def status(queue: StreamQueue, key: str) -> str | None:
    job = await queue.job(key)
    return job.status if job else None


async def test_enqueue_dedupes_and_lists_by_state(queues: Any) -> None:
    queue: StreamQueue = await queues()
    assert await queue.enqueue("f", key="a", timeout=5, retries=2, x=1) is not None
    assert await queue.enqueue("f", key="a") is None  # đang chờ: không xếp thêm
    assert await queue.enqueue("f", key="b", scheduled=time.time() + 3600) is not None
    info = await queue.info(jobs=True)
    assert (info["queued"], info["active"], info["scheduled"]) == (1, 0, 1)
    assert [j["key"] for j in info["jobs"]] == ["a", "b"]
    assert [j["key"] for j in (await queue.info(jobs=True, offset=1, limit=1))["jobs"]] == ["b"]
    job = await queue.job("a")
    assert job is not None
    assert (job.function, job.kwargs, job.timeout, job.retries) == ("f", {"x": 1}, 5, 2)
    assert (job.status, job.attempts) == ("queued", 0)
    assert job.queued > 0


async def test_scheduled_job_enters_stream_when_due(queues: Any) -> None:
    queue: StreamQueue = await queues()
    at = time.time() + 0.5
    await queue.enqueue("f", key="soon", scheduled=at)
    assert await queue.schedule() == 0
    assert await status(queue, "soon") == "scheduled"
    await asyncio.sleep(0.7)
    assert await queue.schedule() == 1
    info = await queue.info()
    assert (info["queued"], info["scheduled"]) == (1, 0)
    job = await queue.job("soon")
    assert job is not None
    assert (job.status, job.scheduled) == ("queued", int(at))


async def test_workers_share_slots_and_store_results(queues: Any) -> None:
    first: StreamQueue = await queues()
    second: StreamQueue = await queues()
    now = peak = 0

    async def work(ctx: dict[str, Any], *, n: int) -> dict[str, Any]:
        nonlocal now, peak
        now += 1
        peak = max(peak, now)
        await asyncio.sleep(0.3)
        now -= 1
        return {"n": n, "worker": ctx["worker"].id}

    workers = [StreamWorker(q, functions=[("work", work)], concurrency=3) for q in (first, second)]
    for n in range(12):
        await first.enqueue("work", key=f"w{n}", timeout=10, n=n)
    tasks = [asyncio.create_task(w.start()) for w in workers]
    try:
        await until(lambda: _all_status(first, [f"w{n}" for n in range(12)], "complete"))
        info = await first.info()
        assert len(info["workers"]) == 2
    finally:
        for w, t in zip(workers, tasks, strict=True):
            await stop(w, t)
    assert peak == 6  # 2 worker × 3 slot, không quá
    results = [await first.job(f"w{n}") for n in range(12)]
    assert [r.result["n"] for r in results if r] == list(range(12))
    by_worker = {r.result["worker"] for r in results if r}
    assert by_worker == {w.id for w in workers}  # cả hai worker cùng nhận việc
    info = await first.info()
    assert (info["queued"], info["active"]) == (0, 0)


async def _all_status(queue: StreamQueue, keys: list[str], want: str) -> bool:
    return all([await status(queue, k) == want for k in keys])


async def test_timeout_and_error_retry_then_fail(queues: Any) -> None:
    queue: StreamQueue = await queues()
    attempts: dict[str, list[int]] = {"slow": [], "boom": []}

    async def slow(ctx: dict[str, Any]) -> None:
        attempts["slow"].append(ctx["job"].attempts)
        await asyncio.sleep(10)

    async def boom(ctx: dict[str, Any]) -> None:
        attempts["boom"].append(ctx["job"].attempts)
        raise ValueError("hỏng")

    w = StreamWorker(
        queue,
        functions=[("slow", slow), ("boom", boom)],
        concurrency=2,
        cancellation_hard_deadline_s=1,
    )
    await queue.enqueue("slow", key="slow", timeout=1, retries=2)
    await queue.enqueue("boom", key="boom", timeout=5, retries=2)
    task = asyncio.create_task(w.start())
    try:
        await until(lambda: _all_status(queue, ["slow", "boom"], "failed"))
    finally:
        await stop(w, task)
    assert attempts == {"slow": [1, 2], "boom": [1, 2]}
    slow_job, boom_job = await queue.job("slow"), await queue.job("boom")
    assert slow_job is not None
    assert boom_job is not None
    assert slow_job.error == "quá timeout 1 s"
    assert boom_job.error == "ValueError: hỏng"
    assert (w.stats["retried"], w.stats["failed"]) == (2, 2)


async def test_dead_worker_job_is_reclaimed_and_rerun(queues: Any) -> None:
    queue: StreamQueue = await queues(reclaim_seconds=3)
    await queue.enqueue("work", key="k", timeout=30, retries=2)
    # Worker "chết": nhận job rồi im lặng (không báo sống, không ack).
    [(entry, key)] = await queue.read("dead", 1, 100)
    job = await queue.start(key, entry, "dead")
    assert job is not None
    assert (job.status, job.attempts, job.worker_id) == ("active", 1, "dead")
    ran: list[int] = []

    async def work(ctx: dict[str, Any]) -> None:
        ran.append(ctx["job"].attempts)

    w = StreamWorker(queue, functions=[("work", work)], concurrency=1)
    task = asyncio.create_task(w.start())
    try:
        waited = await until(lambda: _all_status(queue, ["k"], "complete"))
    finally:
        await stop(w, task)
    assert ran == [2]  # lượt của worker chết vẫn tính
    assert waited < 3 + 3  # ngưỡng thu hồi + một vòng thu hồi
    done = await queue.job("k")
    assert done is not None
    assert done.worker_id == w.id
    assert w.stats["retried"] == 1


async def test_active_job_without_pending_entry_is_recovered(queues: Any) -> None:
    """Mất PEL (vd. AOF cụt đoạn cuối): job còn `active`, entry không còn trong group."""
    queue: StreamQueue = await queues(reclaim_seconds=3)
    await queue.enqueue("work", key="lost", timeout=30, retries=2)
    [(entry, key)] = await queue.read("dead", 1, 100)
    assert await queue.start(key, entry, "dead") is not None
    await queue.redis.xack(queue.stream, GROUP, entry)
    ran: list[int] = []

    async def work(ctx: dict[str, Any]) -> None:
        ran.append(ctx["job"].attempts)

    w = StreamWorker(queue, functions=[("work", work)], concurrency=1)
    task = asyncio.create_task(w.start())
    try:
        await until(lambda: _all_status(queue, ["lost"], "complete"))
    finally:
        await stop(w, task)
    assert ran == [2]


async def test_stop_requeues_running_job_without_counting(queues: Any) -> None:
    queue: StreamQueue = await queues()
    started = asyncio.Event()
    cancelled: list[bool] = []

    async def long(ctx: dict[str, Any]) -> None:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(True)  # như process_company: còn kịp ghi lượt chạy
            raise

    w = StreamWorker(
        queue,
        functions=[("long", long)],
        concurrency=1,
        shutdown_grace_s=0.1,
        cancellation_hard_deadline_s=1,
    )
    await queue.enqueue("long", key="long", timeout=60, retries=1)
    task = asyncio.create_task(w.start())
    await asyncio.wait_for(started.wait(), 10)
    await stop(w, task)
    assert cancelled == [True]
    job = await queue.job("long")
    assert job is not None
    assert (job.status, job.attempts, job.error) == ("queued", 0, "worker dừng")
    info = await queue.info()
    assert (info["queued"], info["active"], info["workers"]) == (1, 0, {})


async def test_cron_schedules_one_job_per_tick(queues: Any) -> None:
    async def tick(ctx: dict[str, Any]) -> None:
        return None

    workers = [
        StreamWorker(
            await queues(), functions=[], cron_jobs=[CronJob(tick, cron="*/5 * * * *", timeout=7)]
        )
        for _ in range(2)
    ]
    assert set(workers[0].functions) == {"tick"}
    for w in workers:  # hai replica cùng tới mốc
        await w._schedule()
    queue = workers[0].queue
    info = await queue.info(jobs=True)
    assert info["scheduled"] == 1
    job = await queue.job("cron:tick")
    assert job is not None
    when = datetime.fromtimestamp(job.scheduled, UTC)
    assert when > datetime.now(UTC)
    assert (when.minute % 5, when.second, job.timeout) == (0, 0, 7)


async def test_move_jobs_from_saq(redis_url: str, queues: Any) -> None:
    streams: StreamQueue = await queues()
    saq = queue_from_url(redis_url, streams.name)
    await saq.connect()
    later = int(time.time()) + 3600
    try:
        await saq.enqueue(
            "process_company", key="company:a", timeout=180, retries=2, name="A", continuation=0
        )
        await saq.enqueue("process_company", key="company:b:cont1", scheduled=later, name="B")
        await saq.enqueue("maintenance_cron", key="cron:maintenance_cron", scheduled=later)
        assert await move_jobs(saq, streams) == (2, 0)
        assert await move_jobs(saq, streams) == (0, 2)  # chạy lại: không chép trùng
    finally:
        await saq.disconnect()
    a, b = await streams.job("company:a"), await streams.job("company:b:cont1")
    assert a is not None
    assert b is not None
    assert (a.status, a.timeout, a.retries, a.kwargs) == (
        "queued",
        180,
        2,
        {"name": "A", "continuation": 0},
    )
    assert (b.status, b.scheduled) == ("scheduled", later)
    assert await streams.job("cron:maintenance_cron") is None
