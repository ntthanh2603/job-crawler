from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from worker_fakes import (  # pyright: ignore[reportMissingImports]
    FakeGateway,
    ForwardProxy,
    WebServer,
    start_gateway,
)

from jobworker import config
from jobworker.config import WorkerSettings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Biến môi trường trùng tên field của WorkerSettings không được lọt vào test; file trạng
    thái ghi vào thư mục tạm của test; không đọc /run/secrets của máy chạy test."""
    for name in WorkerSettings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.delenv("SECRETS_DIR", raising=False)
    monkeypatch.setenv("WORKER_HEALTH_FILE", str(tmp_path / "health.json"))
    monkeypatch.setattr(config, "DEFAULT_SECRETS_DIR", tmp_path / "no-secrets")


@pytest.fixture(scope="session")
def web() -> Iterator[WebServer]:
    server = WebServer()
    yield server
    server.close()


@pytest.fixture
def forward_proxy() -> Iterator[ForwardProxy]:
    proxy = ForwardProxy()
    yield proxy
    proxy.close()


@pytest.fixture
async def gateway() -> AsyncIterator[tuple[FakeGateway, int]]:
    gw = FakeGateway()
    server, port = await start_gateway(gw)
    yield gw, port
    await server.stop(0)
