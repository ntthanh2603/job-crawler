"""`jobworker run`: dựng `Worker`, nối tín hiệu dừng, chạy tới khi dừng xong.

SIGTERM / Ctrl-C lần đầu: dừng êm (thôi xin việc, làm nốt việc đang chạy, Leave); lần hai: huỷ luôn
việc đang chạy. Watchdog (WORKER_WATCHDOG_SECONDS) chạy trong `Worker.run`.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from typing import Any

from jobworker.common.config import WorkerSettings
from jobworker.modules.gateway.worker_service import Worker


def install_signal_handlers(worker: Worker) -> None:
    """SIGTERM/SIGINT → `worker.stop()`; lần hai → huỷ việc đang chạy."""
    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, worker.stop, sig.name)
        return

    def handler(signum: int, _frame: Any) -> None:
        loop.call_soon_threadsafe(worker.stop, signal.Signals(signum).name)

    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
        if sig is not None:
            signal.signal(sig, handler)


async def run_worker(settings: WorkerSettings) -> None:
    worker = Worker(settings)
    install_signal_handlers(worker)
    await worker.run()
