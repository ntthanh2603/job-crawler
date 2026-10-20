"""Watchdog trong process: process treo thì tự thoát để Docker (`restart: unless-stopped`) dựng lại.

Docker không tự khởi động lại container `unhealthy`: healthcheck (`jobcrawler health`, `/healthz`,
`gateway-health`) chỉ báo trạng thái. Watchdog chạy ngay trong process api / gateway / scheduler /
runner và gọi `os._exit(EXIT_CODE)` (70, EX_SOFTWARE) sau log `watchdog.stall` khi:

- event loop đứng (`what="event_loop"`): task ghi `time.monotonic()` mỗi giây không được chạy quá
  `max_stall` giây — một lệnh đồng bộ chiếm event loop (vòng lặp CPU, I/O chặn, khoá). Log kèm
  `stack` của luồng event loop (chỗ đang kẹt).
- vòng lặp chính đứng (`what="main_loop"`, `part` = vòng lặp nào; chỉ scheduler và runner):
  `loop_iteration(part)` không được gọi quá `max_stall` giây — vòng lặp nhịp scheduler / vòng lặp
  nhận việc của một vai runner treo ở một `await` không bao giờ xong. Vòng lặp gọi `loop_iteration`
  ở MỌI vòng, kể cả vòng lỗi: mất Postgres / Redis (nhịp lỗi, thử lại) KHÔNG làm thoát (heartbeat
  health vẫn cũ dần như trước), chỉ vòng lặp đứng hẳn mới làm thoát.

Luồng canh là daemon thread (không cần event loop), xét mỗi vài giây. Thoát có chủ đích (SIGTERM,
Ctrl+C, hết lifespan) thì `stop()` trước khi dọn dẹp: lượt dừng chậm không bị giết (`docker stop`
tự SIGKILL sau `stop_grace_period`). Task ghi mốc bị huỷ (event loop đóng) cũng là dừng.

Dự phòng: luồng canh là luồng Python, cần GIL; mã C giữ GIL mãi (regex của `re` quay lui gần như vô
hạn) thì luồng canh cũng đứng. Khi đó `faulthandler.dump_traceback_later` (luồng C, không cần GIL)
in traceback mọi luồng ra stderr rồi thoát mã 1, sau `max_stall + BACKSTOP_EXTRA_SECONDS` giây.

`WATCHDOG_SECONDS` (mặc định 120) là `max_stall`; 0 = tắt (mọi hàm thành no-op).
"""

from __future__ import annotations

import asyncio
import contextlib
import faulthandler
import os
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterable
from typing import Any, NamedTuple

from jobcrawler.logging import get_logger

log = get_logger(__name__)

EXIT_CODE = 70  # EX_SOFTWARE (sysexits.h): khác 1 / 2 của lỗi thường để biết là watchdog
TICK_SECONDS = 1.0  # event loop ghi mốc mỗi chừng này giây
CHECK_SECONDS = 5.0  # luồng canh xét mỗi chừng này giây
# Ghi log lúc thoát có thể kẹt (stderr nghẽn, lock của log handler đang bị luồng kẹt giữ): quá
# chừng này giây thì thoát luôn, không chờ log.
EXIT_GRACE_SECONDS = 5.0
# Dự phòng faulthandler: thoát sau max_stall + chừng này giây kể từ lần đặt lại gần nhất (đặt lại
# mỗi BACKSTOP_REARM_SECONDS) — luôn muộn hơn luồng canh khi luồng canh chạy được.
BACKSTOP_EXTRA_SECONDS = 60.0
BACKSTOP_REARM_SECONDS = 10.0


class Stall(NamedTuple):
    """Thứ đang đứng: `event_loop` hoặc `main_loop` (`part` = vòng lặp nào), đứng bao lâu."""

    what: str
    stalled_s: float
    part: str | None = None


class Watchdog:
    """Canh event loop (và vòng lặp chính nếu có) của một process; xem docstring của module.

    Dùng một lần: `start()` trong event loop cần canh, `loop_iteration(part)` mỗi vòng của vòng lặp
    chính, `stop()` khi thoát có chủ đích. `start()` / `stop()` gọi từ luồng event loop.
    `loop_iteration` của `part` chưa khai báo ở `parts` thì bắt đầu canh `part` đó từ lần gọi đầu.
    """

    def __init__(
        self,
        component: str,
        *,
        max_stall: float,
        parts: Iterable[str] = (),
        clock: Callable[[], float] = time.monotonic,
        exit_fn: Callable[[int], object] = os._exit,
        tick_interval: float | None = None,
        check_interval: float | None = None,
        backstop: bool = True,
    ) -> None:
        self.component = component
        self.max_stall = max(0.0, max_stall)
        self.enabled = self.max_stall > 0
        # max_stall nhỏ (test, cấu hình lạ) thì ghi mốc / xét dày hơn cho khỏi báo nhầm.
        limit = self.max_stall or TICK_SECONDS
        self.tick_interval = tick_interval or min(TICK_SECONDS, limit / 10)
        self.check_interval = check_interval or min(CHECK_SECONDS, limit / 4)
        self._clock = clock
        self._exit = exit_fn
        self._backstop = backstop
        now = clock()
        self._last_tick = now
        self._iterations: dict[str, float] = dict.fromkeys(parts, now)
        self._stopped = threading.Event()
        self._task: asyncio.Task[None] | None = None
        self._thread: threading.Thread | None = None
        self._loop_thread: int | None = None
        self._armed_at: float | None = None  # lần đặt dự phòng faulthandler gần nhất

    @property
    def running(self) -> bool:
        """Luồng canh còn chạy."""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Bắt đầu canh event loop đang chạy (gọi từ trong nó). Tắt (max_stall = 0), đã chạy hay
        đã dừng thì không làm gì."""
        if not self.enabled or self._task is not None or self._stopped.is_set():
            return
        loop = asyncio.get_running_loop()
        now = self._clock()
        self._last_tick = now
        for part in self._iterations:
            self._iterations[part] = now
        self._loop_thread = threading.get_ident()
        self._task = loop.create_task(self._tick_forever(), name=f"watchdog-{self.component}")
        self._thread = threading.Thread(
            target=self._watch, name=f"watchdog-{self.component}", daemon=True
        )
        self._thread.start()
        log.info(
            "watchdog.start",
            component=self.component,
            max_stall=self.max_stall,
            parts=sorted(self._iterations) or None,
        )

    def tick(self) -> None:
        """Event loop còn quay (task của watchdog gọi mỗi `tick_interval`)."""
        self._last_tick = self._clock()

    def loop_iteration(self, part: str = "main") -> None:
        """Vòng lặp chính `part` vừa quay một vòng. Gọi ở MỌI vòng, thành công hay lỗi."""
        if self.enabled:
            self._iterations[part] = self._clock()

    def check(self) -> Stall | None:
        """Thứ đang đứng quá `max_stall` giây (event loop trước), hoặc None."""
        if not self.enabled:
            return None
        now = self._clock()
        idle = now - self._last_tick
        if idle > self.max_stall:
            return Stall("event_loop", idle)
        # copy(): event loop có thể thêm `part` mới trong lúc luồng canh duyệt.
        for part, last in self._iterations.copy().items():
            if now - last > self.max_stall:
                return Stall("main_loop", now - last, part)
        return None

    def stop(self) -> None:
        """Ngừng canh (thoát có chủ đích). Gọi nhiều lần được."""
        self._halt()
        task = self._task
        if task is not None and not task.done():
            with contextlib.suppress(RuntimeError):  # event loop đã đóng
                task.cancel()

    def _halt(self) -> None:
        if self._stopped.is_set():
            return
        self._stopped.set()
        if self._armed_at is not None:
            self._armed_at = None
            faulthandler.cancel_dump_traceback_later()

    async def _tick_forever(self) -> None:
        try:
            while True:
                self.tick()
                self._arm_backstop()
                await asyncio.sleep(self.tick_interval)
        finally:
            # stop(), hoặc event loop đóng (process đang thoát): ngừng canh, không coi là kẹt.
            self._halt()

    def _arm_backstop(self) -> None:
        if not self._backstop or self._stopped.is_set():
            return
        now = self._clock()
        if self._armed_at is not None and now - self._armed_at < BACKSTOP_REARM_SECONDS:
            return
        try:
            faulthandler.dump_traceback_later(self.max_stall + BACKSTOP_EXTRA_SECONDS, exit=True)
        except Exception as exc:  # stderr không có fileno (bị thay thế): bỏ dự phòng
            self._backstop = False
            log.warning("watchdog.backstop_unavailable", component=self.component, error=repr(exc))
            return
        self._armed_at = now

    def _watch(self) -> None:
        while not self._stopped.wait(self.check_interval):
            stall = self.check()
            if stall is not None and not self._stopped.is_set():
                self._fire(stall)
                return

    def _fire(self, stall: Stall) -> None:
        # Hẹn giờ thoát trước khi ghi log: ghi log kẹt thì vẫn thoát.
        guard = threading.Timer(EXIT_GRACE_SECONDS, self._exit, args=(EXIT_CODE,))
        guard.daemon = True
        guard.start()
        fields: dict[str, Any] = {
            "component": self.component,
            "what": stall.what,
            "stalled_s": round(stall.stalled_s, 1),
            "max_stall": self.max_stall,
        }
        if stall.part is not None:
            fields["part"] = stall.part
        with contextlib.suppress(Exception):
            if stall.what == "event_loop" and (stack := self._loop_stack()):
                fields["stack"] = stack
            log.critical("watchdog.stall", **fields)
            sys.stderr.flush()
        self._exit(EXIT_CODE)
        guard.cancel()  # chỉ tới đây khi `exit_fn` không thoát thật (test)

    def _loop_stack(self) -> str | None:
        """Stack hiện tại của luồng event loop (đang kẹt ở đâu)."""
        if self._loop_thread is None:
            return None
        frame = sys._current_frames().get(self._loop_thread)
        return "".join(traceback.format_stack(frame)) if frame is not None else None


def start_watchdog(component: str, *, max_stall: float, parts: Iterable[str] = ()) -> Watchdog:
    """Tạo và chạy watchdog cho event loop đang chạy (gọi từ trong nó). `parts`: các vòng lặp
    chính cần canh (gọi `loop_iteration(part)` mỗi vòng); trống = chỉ canh event loop.
    `max_stall` = 0: watchdog tắt, mọi hàm là no-op."""
    dog = Watchdog(component, max_stall=max_stall, parts=parts)
    dog.start()
    return dog
