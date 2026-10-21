"""`RenderGate` (`modules/fetch/render_service.py`): tắt hẳn / tạm tắt render khi không mở được
Chromium."""

from __future__ import annotations

import asyncio

import pytest

from jcproto.v1 import crawl_pb2
from jobworker.modules.fetch import render_service
from jobworker.modules.fetch.browser_service import BrowserUnavailableError
from jobworker.modules.fetch.render_service import RenderGate, render_backoff


class Changes:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> None:
        self.count += 1


def test_render_backoff_grows_and_caps() -> None:
    assert [render_backoff(n) for n in range(1, 7)] == [30, 60, 120, 240, 480, 600]
    assert render_backoff(100) == 600
    assert render_backoff(0) == 30


async def test_temporary_failure_pauses_then_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(render_service, "RENDER_BACKOFF_BASE_SECONDS", 0.2)
    changes = Changes()
    gate = RenderGate(on_change=changes)

    kind = gate.failed(BrowserUnavailableError("hết RAM"))
    assert kind == crawl_pb2.ERROR_KIND_OTHER
    assert gate.paused() > 0
    assert (gate.failures, changes.count) == (1, 1)
    reason, retry_at_ms = gate.stats()
    assert "hết RAM" in reason
    assert retry_at_ms > 0

    # Việc render khác cùng đợt lỗi: không tính thêm vào backoff.
    gate.failed(BrowserUnavailableError("hết RAM"))
    assert (gate.failures, changes.count) == (1, 1)

    await asyncio.sleep(0.3)  # hết hạn tạm tắt: Register lại với đủ slot render
    assert gate.paused() == 0
    assert changes.count == 2
    assert gate.stats() == ("", 0)

    gate.succeeded()
    assert gate.failures == 0
    gate.close()


async def test_permanent_failure_turns_render_off() -> None:
    changes = Changes()
    gate = RenderGate(on_change=changes)

    missing = BrowserUnavailableError("Executable doesn't exist", permanent=True)
    assert gate.failed(missing) == crawl_pb2.ERROR_KIND_UNSUPPORTED
    assert gate.failed(missing) == crawl_pb2.ERROR_KIND_UNSUPPORTED
    assert gate.off
    assert changes.count == 1
    assert gate.stats()[0].startswith("không có Chromium")
    assert gate.stats()[1] == 0


async def test_close_stops_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(render_service, "RENDER_BACKOFF_BASE_SECONDS", 0.1)
    changes = Changes()
    gate = RenderGate(on_change=changes)

    gate.failed(BrowserUnavailableError("lỗi"))
    gate.close()  # worker dừng / drain
    await asyncio.sleep(0.2)
    assert changes.count == 1


async def test_failure_after_close_does_not_reregister() -> None:
    """Worker đã thôi xin việc (dừng / drain): lỗi Chromium của việc còn dở không đòi Register
    lại."""
    changes = Changes()
    gate = RenderGate(on_change=changes)
    gate.close()
    gate.failed(BrowserUnavailableError("hết RAM"))
    gate.failed(BrowserUnavailableError("Executable doesn't exist", permanent=True))
    assert gate.off
    assert changes.count == 0
