from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
from worker_fakes import (  # pyright: ignore[reportMissingImports]
    FakeGateway,
    ForwardProxy,
    WebServer,
    start_gateway,
)

from jobworker.config import WorkerSettings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Biến môi trường trùng tên field của WorkerSettings không được lọt vào test."""
    for name in WorkerSettings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


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
