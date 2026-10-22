"""Heartbeat health của scheduler / runner và lệnh `jobcrawler health`."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from redis.asyncio import Redis
from typer.testing import CliRunner

from jobcrawler.bootstrap.cli import app
from jobcrawler.services.redis import heartbeat, streamqueue
from jobcrawler.services.redis.heartbeat import Heartbeat, check, heartbeat_key, runner_component
from jobcrawler.services.redis.streamqueue import StreamWorker

from .conftest import FakeClock

PREFIX = "jc-test"


class FakeRedis:
    """GET / SET (kèm `ex`) trong bộ nhớ; `broken` → lỗi như mất kết nối."""

    def __init__(self, *, broken: bool = False) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.broken = broken
        self.closed = False

    async def set(self, key: str, value: str, ex: int | None = None) -> bool:
        if self.broken:
            raise ConnectionError("redis down")
        self.values[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def get(self, key: str) -> bytes | None:
        if self.broken:
            raise ConnectionError("redis down")
        value = self.values.get(key)
        return None if value is None else value.encode()

    async def aclose(self) -> None:
        self.closed = True


def test_component_names() -> None:
    assert runner_component(["build", "poll", "boards"]) == "runner"
    assert runner_component(["boards", "poll", "build"]) == "runner"
    assert runner_component(["poll"]) == "runner-poll"
    assert runner_component(["build"]) == "runner-build"
    assert runner_component(["boards"]) == "runner-boards"
    assert runner_component(["boards", "build"]) == "runner-build-boards"
    assert set(heartbeat.components()) >= {
        "scheduler",
        "runner",
        "runner-build",
        "runner-poll",
        "runner-boards",
    }


def test_key_uses_prefix_component_and_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(heartbeat.socket, "gethostname", lambda: "c0ffee")
    assert heartbeat_key("jc", "runner-poll") == "jc:health:runner-poll:c0ffee"
    assert heartbeat_key("jc", "scheduler", "other") == "jc:health:scheduler:other"


async def test_tick_writes_at_most_every_interval(clock: FakeClock) -> None:
    redis = FakeRedis()
    beat = Heartbeat(redis, PREFIX, "scheduler", host="h1", interval=5, ttl=120, clock=clock)
    assert await beat.tick()
    key = f"{PREFIX}:health:scheduler:h1"
    assert redis.values[key] == "1000.000"
    assert redis.ttls[key] == 120
    clock.now += 4
    assert not await beat.tick()  # chưa đủ nhịp: không ghi
    assert redis.values[key] == "1000.000"
    clock.now += 1
    assert await beat.tick()
    assert redis.values[key] == "1005.000"


async def test_tick_waits_for_every_part(clock: FakeClock) -> None:
    """Runner nhiều vai: một vòng lặp kẹt thì không ghi heartbeat dù vai khác vẫn quay."""
    redis = FakeRedis()
    beat = Heartbeat(redis, PREFIX, "runner", parts=["build", "poll"], host="h", clock=clock)
    assert not await beat.tick("build")
    assert await beat.tick("poll")
    for _ in range(5):
        clock.now += 10
        assert not await beat.tick("build")  # vòng lặp poll kẹt
    assert redis.values[f"{PREFIX}:health:runner:h"] == "1000.000"
    assert await beat.tick("poll")
    assert redis.values[f"{PREFIX}:health:runner:h"] == "1050.000"


async def test_tick_survives_redis_errors(clock: FakeClock) -> None:
    beat = Heartbeat(FakeRedis(broken=True), PREFIX, "scheduler", host="h", clock=clock)
    assert not await beat.tick()


@pytest.mark.parametrize(
    ("stored", "now", "ok", "reason"),
    [
        (None, 1000.0, False, "không có heartbeat"),
        ("995.5", 1000.0, True, "ok scheduler"),
        ("980.0", 1000.0, False, "heartbeat cũ 20.0 s > 15 s"),
        ("abc", 1000.0, False, "heartbeat hỏng"),
    ],
)
async def test_check(stored: str | None, now: float, ok: bool, reason: str) -> None:
    redis = FakeRedis()
    if stored is not None:
        redis.values[f"{PREFIX}:health:scheduler:h"] = stored
    result = await check(redis, PREFIX, "scheduler", host="h", now=now)
    assert result[0] is ok
    assert reason in result[1]


@pytest.mark.parametrize(
    ("components", "ok"),
    [
        (["runner"], True),  # một process đủ ba vai
        (["runner-build", "runner-poll", "runner-boards"], True),  # mỗi vai một process
        (["runner-build-poll", "runner-boards"], True),
        (["runner-build", "runner-boards"], False),  # không còn process poll
        ([], False),
    ],
)
async def test_runner_check_covers_every_role(components: list[str], ok: bool) -> None:
    redis = FakeRedis()
    for c in components:
        redis.values[f"{PREFIX}:health:{c}:h"] = "999.0"
    result = await check(redis, PREFIX, "runner", host="h", now=1000.0)
    assert result[0] is ok, result[1]
    if not ok and components:
        assert "poll" in result[1]


# --- StreamWorker gọi on_loop từ vòng lặp nhận việc --------------------------------------------


class IdleQueue:
    """Hàng đợi rỗng: `read` chờ một chút rồi không trả gì."""

    name = "q-test"
    reclaim_seconds = 60.0

    def __init__(self, *, broken: bool = False) -> None:
        self.broken = broken

    async def read(self, consumer: str, count: int, block_ms: int) -> list[Any]:
        await asyncio.sleep(0.01)
        if self.broken:
            raise ConnectionError("redis down")
        return []


async def _run_briefly(worker: StreamWorker, seconds: float = 0.3) -> None:
    task = asyncio.create_task(worker._fetch())
    await asyncio.sleep(seconds)
    worker.stop()
    await asyncio.wait_for(task, 2)


async def test_worker_loop_calls_on_loop_even_when_slots_are_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(streamqueue, "BLOCK_MS", 20)
    ticks: list[int] = []

    async def on_loop() -> None:
        ticks.append(1)

    worker = StreamWorker(IdleQueue(), functions=[], concurrency=1, on_loop=on_loop)  # pyright: ignore[reportArgumentType]
    await _run_briefly(worker)
    idle_ticks = len(ticks)
    assert idle_ticks >= 3

    # Mọi slot đang bận (job dài): vòng lặp chờ slot nhưng vẫn quay mỗi BLOCK_MS.
    ticks.clear()
    busy = StreamWorker(IdleQueue(), functions=[], concurrency=1, on_loop=on_loop)  # pyright: ignore[reportArgumentType]
    long_job = asyncio.create_task(asyncio.sleep(10))
    busy._running["1-0"] = long_job  # pyright: ignore[reportArgumentType]
    try:
        await _run_briefly(busy)
    finally:
        long_job.cancel()
    assert len(ticks) >= 3


async def test_worker_loop_does_not_beat_when_reads_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticks: list[int] = []

    async def on_loop() -> None:
        ticks.append(1)

    worker = StreamWorker(IdleQueue(broken=True), functions=[], on_loop=on_loop)  # pyright: ignore[reportArgumentType]
    await _run_briefly(worker, 0.1)
    assert ticks == []


# --- jobcrawler health ---------------------------------------------------------------------------

runner = CliRunner()


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch, clean_env: None) -> Iterator[FakeRedis]:
    from jobcrawler.common.config import get_settings

    fake = FakeRedis()
    monkeypatch.setenv("REDIS_PREFIX", PREFIX)
    monkeypatch.setattr(Redis, "from_url", classmethod(lambda cls, url, **kw: fake))
    monkeypatch.setattr(heartbeat.socket, "gethostname", lambda: "box")
    get_settings.cache_clear()
    yield fake
    get_settings.cache_clear()


def test_health_cli_ok_and_stale(fake_redis: FakeRedis) -> None:
    import time

    key = f"{PREFIX}:health:runner-poll:box"
    fake_redis.values[key] = f"{time.time() - 2:.3f}"
    ok = runner.invoke(app, ["health", "runner-poll"])
    assert ok.exit_code == 0, ok.output
    assert "ok runner-poll" in ok.output
    assert fake_redis.closed

    fake_redis.values[key] = f"{time.time() - 60:.3f}"
    stale = runner.invoke(app, ["health", "runner-poll"])
    assert stale.exit_code == 1
    assert "heartbeat cũ" in stale.output
    relaxed = runner.invoke(app, ["health", "runner-poll", "--max-age", "120"])
    assert relaxed.exit_code == 0, relaxed.output


def test_health_cli_missing_unknown_and_redis_down(fake_redis: FakeRedis) -> None:
    missing = runner.invoke(app, ["health", "scheduler"])
    assert missing.exit_code == 1
    assert "không có heartbeat" in missing.output
    unknown = runner.invoke(app, ["health", "runner-xyz"])
    assert unknown.exit_code == 1
    assert "không hợp lệ" in unknown.output
    fake_redis.broken = True
    down = runner.invoke(app, ["health", "scheduler"])
    assert down.exit_code == 1
    assert "Không đọc được Redis" in down.output


def test_health_cli_several_components(fake_redis: FakeRedis) -> None:
    """`jobcrawler health scheduler runner` (container `backend`): khoẻ khi mọi component khoẻ."""
    import time

    now = f"{time.time() - 1:.3f}"
    fake_redis.values[f"{PREFIX}:health:scheduler:box"] = now
    one = runner.invoke(app, ["health", "scheduler", "runner"])
    assert one.exit_code == 1
    assert "ok scheduler" in one.output
    assert "không có heartbeat" in one.output

    fake_redis.values[f"{PREFIX}:health:runner:box"] = now
    both = runner.invoke(app, ["health", "scheduler", "runner"])
    assert both.exit_code == 0, both.output
    assert "ok runner" in both.output


async def test_resize_changes_how_many_jobs_the_loop_asks_for() -> None:
    """Số job song song đổi lúc chạy (runner theo tổng slot của worker đang kết nối): tăng thì
    vòng nhận việc xin thêm job ngay, không phải khởi động lại."""

    class CountingQueue(IdleQueue):
        def __init__(self) -> None:
            super().__init__()
            self.counts: list[int] = []

        async def read(self, consumer: str, count: int, block_ms: int) -> list[Any]:
            self.counts.append(count)
            return await super().read(consumer, count, block_ms)

    sizes = iter([3, 7])

    async def resize() -> int:
        return next(sizes)

    queue = CountingQueue()
    worker = StreamWorker(queue, functions=[], concurrency=1, resize=resize)  # pyright: ignore[reportArgumentType]
    await worker._resize()
    assert worker.concurrency == 3
    task = asyncio.create_task(worker._fetch())
    await asyncio.sleep(0.05)
    await worker._resize()
    await asyncio.sleep(0.05)
    worker.stop()
    await asyncio.wait_for(task, 2)
    assert queue.counts[0] == 3
    assert queue.counts[-1] == 7
