"""File trạng thái + `jobworker health` (healthcheck của Docker)."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import grpc
import pytest
from typer.testing import CliRunner
from worker_fakes import (  # pyright: ignore[reportMissingImports]
    FakeGateway,
    RunningWorker,
    closed_port,
    make_settings,
    stop_gateway,
)

from jcproto.v1 import crawl_pb2_grpc
from jobworker.bootstrap.cli import app
from jobworker.modules.gateway.worker_service import Worker
from jobworker.modules.health import status_service
from jobworker.modules.health.status_service import (
    DRAINING,
    RECONNECTING,
    RUNNING,
    STARTING,
    HealthStatus,
    check,
)

Gateway = tuple[FakeGateway, int]


def read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_status(path: Path, **kw: Any) -> None:
    data: dict[str, Any] = {
        "worker_id": "w1",
        "pid": 1,
        "state": RUNNING,
        "updated_at": 1000.0,
        "last_ok_at": 1000.0,
        "disconnected_since": None,
        "heartbeat_seconds": 5.0,
    }
    data.update(kw)
    path.write_text(json.dumps(data), encoding="utf-8")


# --- ghi file ------------------------------------------------------------------------------------


def test_status_transitions_are_written(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "health.json"  # thư mục cha tự tạo
    status = HealthStatus(path, "w1", heartbeat_seconds=5.0)
    status.write()
    data = read(path)
    assert data["state"] == STARTING
    assert data["worker_id"] == "w1"
    assert data["pid"] == os.getpid()
    assert data["heartbeat_seconds"] == 5.0
    assert data["last_ok_at"] is None
    assert data["disconnected_since"] is not None

    status.ok()
    data = read(path)
    assert data["state"] == RUNNING
    assert data["disconnected_since"] is None
    assert data["last_ok_at"] is not None

    status.failed()
    first = read(path)
    assert first["state"] == RECONNECTING
    status.failed()  # lỗi tiếp: giữ mốc lỗi đầu tiên
    assert status.disconnected_since == first["disconnected_since"]

    status.ok()
    assert read(path)["state"] == RUNNING
    status.drain()
    status.failed()  # đang drain: vẫn là draining
    assert read(path)["state"] == DRAINING
    # Ghi nguyên tử: không để lại file tạm.
    assert sorted(p.name for p in path.parent.iterdir()) == ["health.json"]

    status.remove()
    assert not path.exists()
    status.remove()  # không có file: không lỗi


def test_same_state_is_throttled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "health.json"
    status = HealthStatus(path, "w1", heartbeat_seconds=5.0)
    status.ok()
    written = read(path)["updated_at"]
    status.ok()
    status.touch()
    assert read(path)["updated_at"] == written  # trong WRITE_INTERVAL_SECONDS: không ghi lại
    monkeypatch.setattr(status_service, "WRITE_INTERVAL_SECONDS", 0.0)
    status.touch()
    assert read(path)["updated_at"] >= written


def test_write_error_does_not_raise(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    status = HealthStatus(blocker / "health.json", "w1", heartbeat_seconds=5.0)
    status.write()  # thư mục cha là file: OSError bị nuốt, chỉ cảnh báo
    status.ok()
    assert status.state == RUNNING


# --- đánh giá ------------------------------------------------------------------------------------


def test_check_missing_and_corrupt(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    ok, reason = check(path)
    assert not ok
    assert "không có file" in reason
    path.write_text("{nửa file", encoding="utf-8")
    ok, reason = check(path)
    assert not ok
    assert "không đọc được" in reason
    path.write_text('{"state": "running"}', encoding="utf-8")
    ok, reason = check(path)
    assert not ok
    assert "sai định dạng" in reason


def test_check_freshness(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    write_status(path, updated_at=1000.0, heartbeat_seconds=5.0)
    assert check(path, now=1029.0) == (True, "w1: running, cập nhật 29 s trước")
    ok, reason = check(path, now=1031.0)  # mặc định max(30, 3 × 5) = 30 s
    assert not ok
    assert "không cập nhật 31 s" in reason
    assert check(path, now=1031.0, max_age=60)[0]
    # Gateway đặt chu kỳ heartbeat dài: hạn mặc định giãn theo (3 × 20 = 60 s).
    write_status(path, updated_at=1000.0, heartbeat_seconds=20.0)
    assert check(path, now=1059.0)[0]
    assert not check(path, now=1061.0)[0]


@pytest.mark.parametrize("state", [STARTING, RECONNECTING])
def test_check_gateway_grace(tmp_path: Path, state: str) -> None:
    path = tmp_path / "health.json"
    write_status(path, state=state, updated_at=1000.0, disconnected_since=700.0)
    # Mất gateway 299 s: vẫn khoẻ (gateway khởi động lại không kéo theo mọi worker).
    assert check(path, now=999.0)[0]
    ok, reason = check(path, now=1001.0)
    assert not ok
    assert "không kết nối được gateway 301 s" in reason
    assert check(path, now=1001.0, gateway_grace=0)[0]  # 0 = bỏ qua
    assert not check(path, now=1001.0, gateway_grace=60)[0]


def test_check_draining_and_running_ignore_grace(tmp_path: Path) -> None:
    path = tmp_path / "health.json"
    write_status(path, state=DRAINING, updated_at=1000.0, disconnected_since=0.0)
    assert check(path, now=1001.0)[0]
    write_status(path, state=RUNNING, updated_at=1000.0, disconnected_since=None)
    assert check(path, now=1001.0)[0]


# --- CLI -----------------------------------------------------------------------------------------


def test_cli_health(tmp_path: Path) -> None:
    runner = CliRunner()
    # Mặc định đọc WORKER_HEALTH_FILE (conftest trỏ vào thư mục tạm của test).
    result = runner.invoke(app, ["health"])
    assert result.exit_code == 1
    assert "unhealthy: không có file" in result.output

    status = HealthStatus(tmp_path / "health.json", "w1", heartbeat_seconds=5.0)
    status.ok()
    result = runner.invoke(app, ["health"])
    assert result.exit_code == 0, result.output
    assert "healthy: w1: running" in result.output

    other = tmp_path / "other.json"
    write_status(other, updated_at=1.0)
    result = runner.invoke(app, ["health", "--file", str(other)])
    assert result.exit_code == 1
    assert "không cập nhật" in result.output
    result = runner.invoke(app, ["health", "--file", str(other), "--max-age", "1e12"])
    assert result.exit_code == 0, result.output

    reconnecting = tmp_path / "reconnecting.json"
    write_status(
        reconnecting,
        state=RECONNECTING,
        updated_at=status.last_ok_at,
        disconnected_since=(status.last_ok_at or 0) - 120,
    )
    args = ["health", "--file", str(reconnecting)]
    assert runner.invoke(app, args).exit_code == 0
    assert runner.invoke(app, [*args, "--gateway-grace", "60"]).exit_code == 1


# --- worker thật với gateway giả -----------------------------------------------------------------


async def test_worker_writes_status_and_removes_it(gateway: Gateway, tmp_path: Path) -> None:
    gw, port = gateway
    path = tmp_path / "w.json"
    run = RunningWorker(Worker(make_settings(port, worker_health_file=path)))
    try:
        await gw.wait_for(lambda: path.exists() and read(path)["state"] == RUNNING)
        data = read(path)
        assert data["worker_id"] == "test-worker"
        assert check(path)[0]
    finally:
        await run.stop()
    assert not path.exists()  # dừng hẳn: xoá file


async def test_drain_state(
    gateway: Gateway, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gw, port = gateway
    path = tmp_path / "w.json"
    run = RunningWorker(Worker(make_settings(port, worker_health_file=path)))
    await gw.wait_for(lambda: path.exists() and read(path)["state"] == RUNNING)
    states: list[str] = []
    real_remove = run.worker.health.remove

    def remove() -> None:
        states.append(read(path)["state"])
        real_remove()

    monkeypatch.setattr(run.worker.health, "remove", remove)
    gw.drain = True
    await run.wait()
    assert states == [DRAINING]


async def test_gateway_down_is_starting_then_reconnecting(tmp_path: Path) -> None:
    port = closed_port()
    path = tmp_path / "w.json"
    run = RunningWorker(Worker(make_settings(port, worker_health_file=path)))
    gw = FakeGateway()
    gw.config.heartbeat_seconds = 1
    server = grpc.aio.server()
    try:
        await asyncio.sleep(0.5)
        data = read(path)
        assert data["state"] == STARTING
        assert data["disconnected_since"] is not None
        assert check(path)[0]  # trong hạn 5 phút
        assert not check(path, gateway_grace=0.01)[0]

        crawl_pb2_grpc.add_CrawlGatewayServicer_to_server(gw, server)
        server.add_insecure_port(f"127.0.0.1:{port}")
        await server.start()
        await gw.wait_for(lambda: read(path)["state"] == RUNNING, timeout=20)

        await stop_gateway(server)  # gateway chết khi worker đang chạy
        await gw.wait_for(lambda: read(path)["state"] == RECONNECTING, timeout=20)
        data = read(path)
        assert data["last_ok_at"] is not None
        assert data["disconnected_since"] is not None
    finally:
        await run.stop()
        await stop_gateway(server)
