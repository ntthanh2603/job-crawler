"""Render bật hay tắt theo Chromium.

Không mở được Chromium → tạm tắt render trong min(30 s · 2^(n−1), 10 phút) với n là số lần lỗi liên
tiếp, hết hạn thì bật lại (việc render sau mở lại Chromium); mở được thì n về 0. Máy chưa cài
Chromium thì tắt hẳn. Mỗi lần tắt / hết hạn tạm tắt gọi `on_change` để worker Register lại với số
slot render mới (0 khi đang tắt). Trạng thái báo trong `WorkerStats.render_disabled_reason` /
`render_retry_at_ms`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from jcproto.v1 import crawl_pb2
from jobworker.common.logging import get_logger
from jobworker.modules.fetch.browser_service import BrowserUnavailableError
from jobworker.modules.fetch.results_service import now_ms

log = get_logger(__name__)

# Không mở được Chromium: tạm tắt render 30 s, 60 s, 120 s... tối đa 10 phút.
RENDER_BACKOFF_BASE_SECONDS = 30.0
RENDER_BACKOFF_CAP_SECONDS = 600.0


def render_backoff(failures: int) -> float:
    """Giây tạm tắt render sau `failures` lần liên tiếp không mở được Chromium."""
    delay = RENDER_BACKOFF_BASE_SECONDS * 2 ** min(max(0, failures - 1), 30)
    return min(RENDER_BACKOFF_CAP_SECONDS, delay)


class RenderGate:
    """Render tắt hẳn (`off`: máy chưa cài Chromium) / tạm tắt tới `_retry_at` (monotonic) sau
    `failures` lần liên tiếp không mở được Chromium. Chỉ gọi từ event loop."""

    def __init__(self, *, on_change: Callable[[], None]) -> None:
        self.off = False
        self.failures = 0
        self.reason = ""
        self._retry_at = 0.0
        self._timer: asyncio.TimerHandle | None = None
        self._closed = False
        self._on_change = on_change

    def paused(self) -> float:
        """Số giây còn tạm tắt render (0 = không tạm tắt)."""
        return max(0.0, self._retry_at - time.monotonic())

    def failed(self, exc: BrowserUnavailableError) -> crawl_pb2.ErrorKind:
        """Không mở được Chromium: tắt hẳn (chưa cài) hoặc tạm tắt theo backoff. Trả ErrorKind cho
        kết quả của việc."""
        if exc.permanent:
            if not self.off:
                log.error("worker.browser_unavailable", error=str(exc), permanent=True)
                self.off = True
                self.reason = f"không có Chromium: {exc}"
                self._notify()
            return crawl_pb2.ERROR_KIND_UNSUPPORTED
        # Nhiều việc render cùng chờ mở Chromium: chỉ lần lỗi đầu của đợt tính vào backoff.
        if not self.paused():
            self.failures += 1
            delay = render_backoff(self.failures)
            self._retry_at = time.monotonic() + delay
            self.reason = f"không mở được Chromium: {exc}"
            log.error(
                "worker.browser_unavailable",
                error=str(exc),
                failures=self.failures,
                retry_in=delay,
            )
            self._notify()
            self._cancel_timer()
            if not self._closed:
                self._timer = asyncio.get_running_loop().call_later(delay, self._retry)
        return crawl_pb2.ERROR_KIND_OTHER

    def _notify(self) -> None:
        """Báo worker Register lại với số slot render mới; worker đã thôi xin việc thì thôi."""
        if not self._closed:
            self._on_change()

    def _retry(self) -> None:
        """Hết hạn tạm tắt: Register lại với đủ slot render, việc render sau mở lại Chromium."""
        self._timer = None
        if self._closed or self.off:
            return
        log.info("worker.render_retry", failures=self.failures)
        self._on_change()

    def succeeded(self) -> None:
        """Chromium mở được (trang có lỗi hay không)."""
        if self.failures:
            log.info("worker.browser_recovered", failures=self.failures)
        self.failures = 0
        self._retry_at = 0.0
        self.reason = ""

    def stats(self) -> tuple[str, int]:
        """(`render_disabled_reason`, `render_retry_at_ms`) cho `WorkerStats`."""
        paused = self.paused()
        reason = self.reason if self.off or paused else ""
        return reason[:500], now_ms() + int(paused * 1000) if paused else 0

    def close(self) -> None:
        """Worker thôi xin việc (dừng / drain): không bật lại render nữa."""
        self._closed = True
        self._cancel_timer()

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
