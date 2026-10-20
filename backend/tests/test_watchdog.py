"""Watchdog trong process (`jobcrawler.common.watchdog`): event loop / vòng lặp chính đứng thì thoát
mã 70.

Không thoát thật: `exit_fn` giả ghi lại mã thoát. Test có luồng canh thật dùng `max_stall` vài trăm
ms và chặn event loop bằng `time.sleep` có chủ đích.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from functools import partial
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from jobcrawler.bootstrap import scheduler
from jobcrawler.common import logging as jlog
from jobcrawler.common import watchdog
from jobcrawler.common.config import Settings
from jobcrawler.common.watchdog import EXIT_CODE, Stall, Watchdog, start_watchdog
from jobcrawler.modules.settings import app_settings
from jobcrawler.services.redis import streamqueue
from jobcrawler.services.redis.heartbeat import Heartbeat
from jobcrawler.services.redis.streamqueue import StreamWorker

from .conftest import FakeClock

STALL = 0.3  # max_stall của test có luồng canh thật (giây)


class Exits:
    """`exit_fn` giả: ghi lại mã thoát thay vì thoát."""

    def __init__(self) -> None:
        self.codes: list[int] = []
        self.fired = threading.Event()

    def __call__(self, code: int) -> None:
        self.codes.append(code)
        self.fired.set()


def live_dog(exits: Exits, *, parts: tuple[str, ...] = (), stall: float = STALL) -> Watchdog:
    return Watchdog(
        "test", max_stall=stall, parts=parts, exit_fn=exits, check_interval=0.02, backstop=False
    )


# --- quyết định (đồng hồ giả, không luồng) -------------------------------------------------------


def test_event_loop_stall_detected(clock: FakeClock) -> None:
    dog = Watchdog("api", max_stall=10, clock=clock, backstop=False)
    clock.now += 9
    assert dog.check() is None
    clock.now += 2
    assert dog.check() == Stall("event_loop", 11)
    dog.tick()  # event loop quay lại
    assert dog.check() is None


def test_main_loop_stall_names_the_part(clock: FakeClock) -> None:
    """Runner nhiều vai: vai nào đứng cũng tính, dù event loop và vai khác vẫn quay."""
    dog = Watchdog("runner", max_stall=10, parts=("build", "poll"), clock=clock, backstop=False)
    for _ in range(4):
        clock.now += 3
        dog.tick()
        dog.loop_iteration("build")
    assert dog.check() == Stall("main_loop", 12, "poll")
    dog.loop_iteration("poll")
    assert dog.check() is None


def test_iterations_count_whatever_their_outcome(clock: FakeClock) -> None:
    """Vòng lặp lỗi liên tục (mất Postgres / Redis) nhưng vẫn quay: không bao giờ là đứng."""
    dog = Watchdog("scheduler", max_stall=10, parts=("main",), clock=clock, backstop=False)
    for _ in range(100):
        clock.now += 5  # mỗi vòng: thử, lỗi, chờ 5 s rồi thử lại
        dog.tick()
        dog.loop_iteration()
        assert dog.check() is None


def test_zero_disables(clock: FakeClock) -> None:
    dog = Watchdog("api", max_stall=0, parts=("main",), clock=clock)
    assert not dog.enabled
    clock.now += 1e6
    dog.loop_iteration()
    assert dog.check() is None


async def test_start_watchdog_zero_starts_nothing() -> None:
    dog = start_watchdog("api", max_stall=0, parts=("main",))
    assert not dog.enabled
    assert not dog.running
    dog.loop_iteration()
    dog.stop()


# --- luồng canh thật -----------------------------------------------------------------------------


@pytest.mark.usefixtures("clean_env")
async def test_blocked_event_loop_exits_70(capsys: pytest.CaptureFixture[str]) -> None:
    jlog.configure_logging(Settings(_env_file=None, log_format="json"))  # pyright: ignore[reportCallIssue]
    exits = Exits()
    dog = live_dog(exits)
    dog.start()
    try:
        await asyncio.sleep(0.05)
        time.sleep(1.0)  # noqa: ASYNC251 — chặn event loop có chủ đích
        assert exits.fired.wait(2)
    finally:
        dog.stop()
    assert exits.codes == [EXIT_CODE] == [70]
    assert not dog.running
    lines = [json.loads(x) for x in capsys.readouterr().err.splitlines() if "watchdog.stall" in x]
    [event] = lines
    assert event["level"] == "critical"
    assert event["what"] == "event_loop"
    assert event["component"] == "test"
    assert event["stalled_s"] >= STALL  # làm tròn 0.1 s
    assert "test_blocked_event_loop_exits_70" in event["stack"]  # chỗ event loop đang kẹt


async def test_stuck_main_loop_exits_70() -> None:
    """Vòng lặp nhận việc của vai `poll` treo ở một await (event loop vẫn quay): thoát."""

    class HangingQueue:
        name = "q-test"
        reclaim_seconds = 60.0

        async def read(self, consumer: str, count: int, block_ms: int) -> list[Any]:
            await asyncio.Event().wait()  # không bao giờ xong
            return []

    exits = Exits()
    dog = live_dog(exits, parts=("build", "poll"))
    dog.start()
    busy = StreamWorker(HangingQueue(), functions=[])  # pyright: ignore[reportArgumentType]
    busy.on_iteration = partial(dog.loop_iteration, "poll")
    task = asyncio.create_task(busy._fetch())
    try:
        for _ in range(60):  # vai build vẫn quay
            dog.loop_iteration("build")
            await asyncio.sleep(0.02)
            if exits.fired.is_set():
                break
        stall = dog.check()
        assert stall is not None
        assert (stall.what, stall.part) == ("main_loop", "poll")
    finally:
        dog.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert exits.codes == [EXIT_CODE]


async def test_worker_read_errors_keep_iterating(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redis mất: vòng lặp nhận việc không báo heartbeat (`on_loop`) nhưng vẫn báo watchdog."""

    class DownQueue:
        name = "q-test"
        reclaim_seconds = 60.0

        async def read(self, consumer: str, count: int, block_ms: int) -> list[Any]:
            raise ConnectionError("redis down")

    monkeypatch.setattr(streamqueue, "READ_RETRY_SECONDS", 0.02)
    beats: list[int] = []

    async def on_loop() -> None:
        beats.append(1)

    exits = Exits()
    dog = live_dog(exits, parts=("poll",))
    dog.start()
    worker = StreamWorker(DownQueue(), functions=[], on_loop=on_loop)  # pyright: ignore[reportArgumentType]
    worker.on_iteration = partial(dog.loop_iteration, "poll")
    task = asyncio.create_task(worker._fetch())
    try:
        await asyncio.sleep(3 * STALL)
    finally:
        dog.stop()
        worker.stop()
        await asyncio.wait_for(task, 2)
    assert exits.codes == []
    assert beats == []  # healthcheck vẫn thấy heartbeat cũ dần


async def test_scheduler_postgres_down_keeps_iterating(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mất Postgres (replica dự phòng, leader là process khác): scheduler hỏi lại Postgres mãi,
    không ghi heartbeat, watchdog không thoát."""

    class DownEngine:
        attempts = 0

        def connect(self) -> Any:
            self.attempts += 1
            raise ConnectionRefusedError("postgres down")

    class Redis:
        def __init__(self) -> None:
            # Khoá leader đang do process khác giữ.
            self.values: dict[str, str] = {"jc:lock:scheduler": "may-khac"}

        async def set(
            self,
            key: str,
            value: str,
            ex: int | None = None,
            nx: bool = False,
            px: int | None = None,
        ) -> bool | None:
            if nx and key in self.values:
                return None
            self.values[key] = value
            return True

        async def eval(self, script: str, numkeys: int, *args: Any) -> int:
            return 0  # gia hạn / nhả: không phải khoá của mình

    monkeypatch.setattr(scheduler, "STANDBY_SECONDS", 0.02)
    redis, engine = Redis(), DownEngine()
    res: Any = SimpleNamespace(settings=Settings(_env_file=None), engine=engine, redis=redis)  # pyright: ignore[reportCallIssue]
    exits = Exits()
    dog = live_dog(exits, parts=("main",))
    dog.start()
    stop = asyncio.Event()
    beat = Heartbeat(redis, "jc-test", "scheduler", host="h")
    task = asyncio.create_task(scheduler.run_loop(res, stop, beat, watchdog=dog))
    try:
        await asyncio.sleep(3 * STALL)
    finally:
        dog.stop()
        stop.set()
        await asyncio.wait_for(task, 2)
    assert exits.codes == []
    assert engine.attempts >= 5
    assert redis.values == {"jc:lock:scheduler": "may-khac"}  # không ghi heartbeat


async def test_graceful_stop_does_not_fire() -> None:
    exits = Exits()
    dog = live_dog(exits, parts=("main",))
    dog.start()
    await asyncio.sleep(0.05)
    dog.stop()
    time.sleep(3 * STALL)  # noqa: ASYNC251 — sau khi dừng: event loop và vòng lặp chính cùng đứng
    await asyncio.sleep(0.05)
    assert exits.codes == []
    assert not dog.running
    dog.stop()  # gọi lại được


async def test_cancelled_tick_task_stops_watching() -> None:
    """Event loop đóng (task bị huỷ khi process thoát) không bị coi là kẹt."""
    exits = Exits()
    dog = live_dog(exits)
    dog.start()
    await asyncio.sleep(0.05)
    assert dog._task is not None
    dog._task.cancel()
    await asyncio.sleep(0.1)
    assert not dog.running
    time.sleep(2 * STALL)  # noqa: ASYNC251
    assert exits.codes == []


async def test_backstop_armed_while_running_and_cancelled_on_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        watchdog.faulthandler,
        "dump_traceback_later",
        lambda timeout, **kw: calls.append(("arm", timeout, kw.get("exit"))),
    )
    monkeypatch.setattr(
        watchdog.faulthandler, "cancel_dump_traceback_later", lambda: calls.append(("cancel",))
    )
    dog = Watchdog("api", max_stall=10, exit_fn=Exits())
    dog.start()
    await asyncio.sleep(0.05)
    dog.stop()
    await asyncio.sleep(0)
    assert calls == [("arm", 10 + watchdog.BACKSTOP_EXTRA_SECONDS, True), ("cancel",)]


# --- cài đặt ------------------------------------------------------------------------------------


@pytest.mark.usefixtures("clean_env")
def test_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings(_env_file=None).watchdog_seconds == 120  # pyright: ignore[reportCallIssue]
    monkeypatch.setenv("WATCHDOG_SECONDS", "0")
    assert Settings(_env_file=None).watchdog_seconds == 0  # pyright: ignore[reportCallIssue]
    monkeypatch.setenv("WATCHDOG_SECONDS", "-1")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert "watchdog_seconds" in app_settings.ENV_ONLY
