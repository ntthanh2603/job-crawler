"""Watchdog: event loop / vòng heartbeat đứng quá hạn → thoát; mất gateway và dừng êm thì không.

`exit_fn` thay `os._exit` bằng `list.append`: test chỉ ghi lại mã thoát.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import grpc
import pytest
from worker_fakes import (  # pyright: ignore[reportMissingImports]
    FakeGateway,
    RunningWorker,
    closed_port,
    make_settings,
    start_gateway,
    stop_gateway,
)

from jcproto.v1 import crawl_pb2, crawl_pb2_grpc
from jobworker.common.watchdog import DRAIN, EVENT_LOOP, EXIT_CODE, MAIN_LOOP, Watchdog
from jobworker.modules.gateway import runtime_service, worker_service
from jobworker.modules.gateway.worker_service import Worker
from jobworker.modules.health.status_service import RECONNECTING, RUNNING, STARTING

Gateway = tuple[FakeGateway, int]


class Clock:
    """Đồng hồ giả cho `Watchdog(clock=...)`."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def manual(limit: float, clock: Clock) -> Watchdog:
    return Watchdog(limit, exit_fn=lambda _code: None, clock=clock)


def live(limit: float, exits: list[int], *, check: float = 0.02) -> Watchdog:
    """Watchdog chạy thật (thread + task tick) với chu kỳ ngắn; thoát = ghi mã vào `exits`."""
    return Watchdog(limit, exit_fn=exits.append, tick_seconds=0.02, check_seconds=check)


async def wait_until(predicate: Callable[[], object], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("hết thời gian chờ điều kiện")
        await asyncio.sleep(0.02)


@pytest.fixture
def fast_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Heartbeat 0.2 s, hạn mỗi lời gọi 0.5 s: hạn vòng chính của watchdog (hạn + 0.2 + 0.5 s)
    ngắn đủ để test chạy nhanh, mà vẫn dài hơn một lượt heartbeat khi gateway chết."""
    monkeypatch.setattr(runtime_service, "DEFAULT_HEARTBEAT_SECONDS", 0.2)
    monkeypatch.setattr(worker_service, "RPC_TIMEOUT_SECONDS", 0.5)


# --- đánh giá (đồng hồ giả) ----------------------------------------------------------------------


def test_event_loop_stall() -> None:
    clock = Clock()
    wd = manual(10, clock)
    wd.beat(1000)  # vòng chính không phải lý do
    clock.now += 10
    assert wd.stall() is None  # đúng bằng hạn: chưa tính
    clock.now += 0.5
    assert wd.stall() == (EVENT_LOOP, 10.5, 10)
    wd.tick()
    assert wd.stall() is None


def test_main_loop_stall_uses_slack() -> None:
    clock = Clock()
    wd = manual(10, clock)
    wd.beat(5)  # lượt sau được phép tới muộn 5 s (chờ nhịp kế + lời gọi Heartbeat)
    clock.now += 15
    wd.tick()
    assert wd.stall() is None
    clock.now += 0.5
    wd.tick()
    assert wd.stall() == (MAIN_LOOP, 15.5, 15)
    wd.beat(5)
    assert wd.stall() is None


def test_drain_ignores_main_loop_until_deadline() -> None:
    clock = Clock()
    wd = manual(10, clock)
    wd.drain(30)  # hạn tổng 30 + 10 s
    assert wd.draining
    clock.now += 20
    wd.drain(1000)  # gọi lại không gia hạn
    clock.now += 20
    wd.tick()
    assert wd.stall() is None  # heartbeat đã dừng 40 s: không tính khi đang dừng
    clock.now += 0.5
    wd.tick()
    assert wd.stall() == (DRAIN, 40.5, 40)

    # Đang dừng mà event loop đứng: vẫn tính.
    wd = manual(10, clock)
    wd.drain(1000)
    clock.now += 10.5
    assert wd.stall() == (EVENT_LOOP, 10.5, 10)


async def test_zero_disables() -> None:
    exits: list[int] = []
    wd = live(0, exits)
    assert not wd.enabled
    wd.start()
    assert not wd.running
    time.sleep(0.1)  # noqa: ASYNC251 — cố ý chặn event loop
    assert wd.stall() is None
    await wd.stop()
    assert exits == []


# --- thread kiểm tra chạy thật -------------------------------------------------------------------


async def test_blocked_event_loop_exits() -> None:
    exits: list[int] = []
    wd = live(0.2, exits)
    wd.start()
    wd.beat(60)  # chỉ event loop là lý do
    try:
        time.sleep(0.8)  # noqa: ASYNC251 — cố ý chặn event loop quá hạn
        await wait_until(lambda: exits)
        assert exits == [EXIT_CODE]
        assert wd.fired == EVENT_LOOP
        await wait_until(lambda: not wd.running)  # gọi thoát rồi thì thread kiểm tra dừng
    finally:
        await wd.stop()
    assert exits == [EXIT_CODE]  # chỉ một lần


async def test_main_loop_stall_exits() -> None:
    exits: list[int] = []
    wd = live(0.2, exits)
    wd.start()
    try:
        # Event loop vẫn chạy (task tick) nhưng không ai gọi `beat`.
        await wait_until(lambda: exits)
        assert exits == [EXIT_CODE]
        assert wd.fired == MAIN_LOOP
    finally:
        await wd.stop()


async def test_stopped_watchdog_never_exits() -> None:
    exits: list[int] = []
    wd = live(0.2, exits)
    wd.start()
    assert wd.running
    await wd.stop()
    assert not wd.running
    time.sleep(0.4)  # noqa: ASYNC251 — event loop "đứng" sau khi đã dừng watchdog
    assert exits == []


# --- worker thật với gateway giả -----------------------------------------------------------------


@pytest.mark.parametrize("seconds", [0, 120])
async def test_worker_watchdog_from_settings(gateway: Gateway, seconds: float) -> None:
    gw, port = gateway
    worker = Worker(make_settings(port, worker_watchdog_seconds=seconds))
    assert worker.watchdog.limit == seconds
    run = RunningWorker(worker)
    try:
        await gw.wait_for(lambda: worker.health.state == RUNNING)
        assert worker.watchdog.running == (seconds > 0)
    finally:
        await run.stop()
    assert not worker.watchdog.running


@pytest.mark.usefixtures("fast_heartbeat")
async def test_stuck_heartbeat_loop_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    never = asyncio.Event()

    async def stuck(timeout: float, *events: asyncio.Event) -> None:
        await never.wait()  # vòng heartbeat kẹt (deadlock giả), event loop vẫn chạy

    monkeypatch.setattr(worker_service, "wait_any", stuck)
    exits: list[int] = []
    wd = live(0.5, exits, check=0.05)
    run = RunningWorker(Worker(make_settings(closed_port()), watchdog=wd))
    try:
        await wait_until(lambda: exits)
        assert exits == [EXIT_CODE]
        assert wd.fired == MAIN_LOOP
    finally:
        await run.stop()


@pytest.mark.usefixtures("fast_heartbeat")
async def test_gateway_down_does_not_exit() -> None:
    port = closed_port()
    exits: list[int] = []
    wd = live(1.0, exits, check=0.05)
    worker = Worker(make_settings(port), watchdog=wd)
    run = RunningWorker(worker)
    server = grpc.aio.server()
    try:
        # Gateway chưa lên: Register thử lại theo backoff, heartbeat vẫn quay.
        await asyncio.sleep(2.0)  # > hạn vòng chính (1 + 0.2 + 0.5 s)
        assert worker.health.state == STARTING
        assert exits == []

        crawl_pb2_grpc.add_CrawlGatewayServicer_to_server(FakeGateway(), server)
        server.add_insecure_port(f"127.0.0.1:{port}")
        await server.start()
        await wait_until(lambda: worker.health.state == RUNNING, timeout=20)

        # Gateway chết khi worker đang chạy: Heartbeat lỗi / hết hạn, lease chờ backoff.
        await stop_gateway(server)
        await wait_until(lambda: worker.health.state == RECONNECTING, timeout=20)
        await asyncio.sleep(2.0)
        assert worker.health.state == RECONNECTING
        assert wd.running
        assert exits == []
    finally:
        await run.stop()
        await stop_gateway(server)
    assert exits == []


class SilentHeartbeatGateway(FakeGateway):
    """Heartbeat không bao giờ trả lời (gateway nghẽn / mạng nuốt gói): mỗi lời gọi chờ hết hạn."""

    def __init__(self) -> None:
        super().__init__()
        self.silent_heartbeats = 0

    async def Heartbeat(self, request: Any, context: Any) -> crawl_pb2.HeartbeatResponse:
        self.silent_heartbeats += 1
        await asyncio.Event().wait()
        raise AssertionError("không tới được")


async def test_silent_gateway_does_not_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_service, "DEFAULT_HEARTBEAT_SECONDS", 0.2)
    # Mỗi Heartbeat chờ hết hạn 1.5 s, lâu hơn cả hạn watchdog (1 s): vòng chính vẫn đúng nhịp.
    monkeypatch.setattr(worker_service, "RPC_TIMEOUT_SECONDS", 1.5)
    gw = SilentHeartbeatGateway()
    server, port = await start_gateway(gw)
    exits: list[int] = []
    wd = live(1.0, exits, check=0.05)
    run = RunningWorker(Worker(make_settings(port), watchdog=wd))
    try:
        await wait_until(lambda: gw.silent_heartbeats >= 3, timeout=20)
        assert wd.running
        assert exits == []
    finally:
        await run.stop()
        await stop_gateway(server)
    assert exits == []


@pytest.mark.usefixtures("fast_heartbeat")
@pytest.mark.parametrize("how", ["signal", "gateway_drain"])
async def test_graceful_stop_does_not_exit(
    gateway: Gateway, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    gw, port = gateway
    exits: list[int] = []
    wd = live(1.0, exits, check=0.05)
    worker = Worker(make_settings(port), watchdog=wd)
    real_close = worker.http.close

    async def slow_close() -> None:
        # Dừng chậm sau khi vòng heartbeat đã tắt: quá hạn vòng chính (1 + 0.2 + 0.5 s) nhưng
        # trong hạn tổng lúc dừng.
        await asyncio.sleep(2.5)
        await real_close()

    monkeypatch.setattr(worker.http, "close", slow_close)
    run = RunningWorker(worker)
    await gw.wait_for(lambda: worker.health.state == RUNNING)
    assert wd.running
    if how == "signal":
        worker.stop("test")
    else:
        gw.drain = True
    await run.wait()
    assert wd.draining
    assert not wd.running
    assert exits == []
