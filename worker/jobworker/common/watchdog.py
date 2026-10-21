"""Watchdog trong process: worker treo thì tự thoát để Docker chạy lại (xem worker/README.md).

Docker (không Swarm) không khởi động lại container `unhealthy`; `restart: unless-stopped` chỉ chạy
lại container khi process thoát. Watchdog biến "treo" thành "thoát" (`WORKER_WATCHDOG_SECONDS`,
mặc định 120 s, 0 = tắt):

- **event loop**: task `_tick_loop` ghi `time.monotonic()` mỗi giây, thread daemon kiểm tra mỗi
  min(5 s, hạn / 4). Loop không chạy quá hạn (lời gọi đồng bộ chặn loop, deadlock trong thư viện
  C...) → thoát.
- **vòng chính**: vòng heartbeat của worker (chỗ làm mới file trạng thái) gọi `beat(slack)` mỗi
  lượt, `slack` là thời gian lượt sau được phép tới muộn (chờ tới nhịp kế + một lời gọi Heartbeat
  có hạn). Quá `slack` + hạn mà không có lượt nào → thoát: vòng kẹt dù loop vẫn chạy. Mất gateway
  không tính: vòng vẫn quay (lời gọi lỗi ngay hoặc hết hạn), lease thử lại theo backoff không bị
  xét.
- **dừng**: `drain(giây)` (đầu `Worker._shutdown`, heartbeat sắp dừng) thôi xét vòng chính, chỉ còn
  hạn tổng = thời gian dừng dự kiến + hạn watchdog; event loop vẫn bị xét. `stop()` khi worker đã
  dừng xong: từ đó không bao giờ thoát vì watchdog.

Thoát: log critical `watchdog.stall`, flush, in stack mọi thread (faulthandler) ra stderr rồi
`os._exit(70)` — không chạy `finally` / atexit: loop đang treo thì không gì chạy tiếp được. Log và
in stack chạy ở thread riêng, chờ tối đa `REPORT_TIMEOUT_SECONDS`: stderr nghẽn cũng không chặn
việc thoát.

Cả process bị đóng băng (máy ngủ, `docker pause`, debugger) thì thread kiểm tra cũng ngủ quá giờ:
bỏ qua lượt đó (cho loop kịp tick lại) thay vì giết worker vừa tỉnh.
"""

from __future__ import annotations

import asyncio
import contextlib
import faulthandler
import logging
import os
import sys
import threading
import time
from collections.abc import Callable

from jobworker.common.logging import get_logger

log = get_logger(__name__)

# Mã thoát khi treo (EX_SOFTWARE): khác 0 / 1 để log và `docker ps -a` phân biệt được.
EXIT_CODE = 70
TICK_SECONDS = 1.0
# Thread kiểm tra mỗi min(CHECK_SECONDS, hạn / 4) giây.
CHECK_SECONDS = 5.0
# Chờ log + in stack tối đa chừng này giây rồi thoát dù stderr nghẽn.
REPORT_TIMEOUT_SECONDS = 5.0

# Loại treo (trường `kind` của `watchdog.stall`).
EVENT_LOOP = "event_loop"
MAIN_LOOP = "main_loop"
DRAIN = "drain"


class Watchdog:
    """Hạn `limit` giây (0 = tắt). `exit_fn`, `clock` và chu kỳ thay được trong test.

    `tick` / `beat` / `drain` gọi từ event loop; `stall` đọc từ thread kiểm tra (mỗi trạng thái là
    một lần gán, thread đọc không bao giờ thấy nửa chừng).
    """

    def __init__(
        self,
        limit: float,
        *,
        exit_fn: Callable[[int], object] = os._exit,
        clock: Callable[[], float] = time.monotonic,
        tick_seconds: float = TICK_SECONDS,
        check_seconds: float | None = None,
    ) -> None:
        self.limit = limit
        self._exit = exit_fn
        self._clock = clock
        self._tick_seconds = tick_seconds
        if check_seconds is None:
            check_seconds = min(CHECK_SECONDS, limit / 4)
        self._check_seconds = check_seconds
        now = clock()
        self._ticked_at = now
        # (lần ghé gần nhất của vòng chính, giây lượt sau được phép muộn).
        self._progress = (now, 0.0)
        # (lúc bắt đầu dừng, hạn tổng) khi worker đang dừng.
        self._drain: tuple[float, float] | None = None
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None
        self._ticker: asyncio.Task[None] | None = None
        self.fired: str | None = None  # loại treo đã gọi `exit_fn` (để test / log)

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    @property
    def running(self) -> bool:
        """Thread kiểm tra còn chạy (đã `start`, chưa `stop`, chưa thoát vì treo)."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def draining(self) -> bool:
        return self._drain is not None

    def start(self) -> None:
        """Bắt đầu theo dõi event loop đang chạy. Tắt (`limit` 0) hoặc đã chạy thì không làm gì."""
        if not self.enabled or self._thread is not None:
            return
        self.tick()
        self.beat()
        loop = asyncio.get_running_loop()
        self._ticker = loop.create_task(self._tick_loop(), name="watchdog-tick")
        self._thread = threading.Thread(target=self._watch, name="watchdog", daemon=True)
        self._thread.start()

    async def stop(self) -> None:
        """Thôi theo dõi (worker đã dừng xong): sau đây không bao giờ gọi `exit_fn`."""
        self._stopped.set()
        if self._ticker is not None:
            self._ticker.cancel()
            await asyncio.gather(self._ticker, return_exceptions=True)
            self._ticker = None
        if self._thread is not None:
            # Thread đang chờ `_stopped`: dậy và thoát ngay.
            self._thread.join(timeout=1.0)

    def tick(self) -> None:
        """Event loop vừa chạy (task `_tick_loop` gọi mỗi `tick_seconds`)."""
        self._ticked_at = self._clock()

    def beat(self, slack: float = 0.0) -> None:
        """Vòng chính vừa qua một lượt; lượt sau tới muộn nhất sau `slack` giây (cộng hạn)."""
        self._progress = (self._clock(), max(0.0, slack))

    def drain(self, expected: float) -> None:
        """Worker bắt đầu dừng, dự kiến xong trong `expected` giây: thôi xét vòng chính, hạn tổng
        là `expected` + hạn watchdog. Gọi lại không gia hạn."""
        if self._drain is None:
            self._drain = (self._clock(), max(0.0, expected) + self.limit)

    def stall(self) -> tuple[str, float, float] | None:
        """(loại, số giây đứng, hạn) nếu đang treo, không thì None."""
        if not self.enabled:
            return None
        now = self._clock()
        idle = now - self._ticked_at
        if idle > self.limit:
            return EVENT_LOOP, idle, self.limit
        if self._drain is not None:
            started, budget = self._drain
            elapsed = now - started
            return (DRAIN, elapsed, budget) if elapsed > budget else None
        beat_at, slack = self._progress
        idle = now - beat_at
        if idle > self.limit + slack:
            return MAIN_LOOP, idle, self.limit + slack
        return None

    async def _tick_loop(self) -> None:
        while True:
            self.tick()
            await asyncio.sleep(self._tick_seconds)

    def _watch(self) -> None:
        last = self._clock()
        # Ngủ quá giờ chừng này thì coi như cả process vừa bị đóng băng.
        overslept = 2 * self._check_seconds + self._tick_seconds
        while not self._stopped.wait(self._check_seconds):
            now = self._clock()
            frozen = now - last > overslept
            last = now
            if frozen:
                continue
            stall = self.stall()
            if stall is not None and not self._stopped.is_set():
                self._fire(*stall)
                return

    def _fire(self, kind: str, idle: float, limit: float) -> None:
        self.fired = kind
        reporter = threading.Thread(
            target=_report, args=(kind, idle, limit), name="watchdog-report", daemon=True
        )
        reporter.start()
        reporter.join(REPORT_TIMEOUT_SECONDS)
        self._exit(EXIT_CODE)


def _report(kind: str, idle: float, limit: float) -> None:
    """Log lý do, flush, rồi in stack mọi thread (faulthandler ghi thẳng vào fd của stderr nên
    flush bộ đệm Python trước để giữ thứ tự)."""
    with contextlib.suppress(Exception):
        log.critical(
            "watchdog.stall",
            kind=kind,
            idle_seconds=round(idle, 1),
            limit_seconds=round(limit, 1),
            exit_code=EXIT_CODE,
        )
    _flush()
    with contextlib.suppress(Exception):
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
    _flush()


def _flush() -> None:
    for handler in logging.getLogger().handlers:
        with contextlib.suppress(Exception):
            handler.flush()
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.flush()
