"""Scheduler: xếp build đêm và poll ngày (services/schedule.py) mỗi SCHEDULER_TICK_SECONDS.

Chạy nhiều replica được: chỉ replica giữ khoá Postgres (`pg_try_advisory_lock`, trên một kết nối
giữ suốt đời process) làm việc, các replica khác chờ và thay khi nó chết (kết nối đứt thì Postgres
nhả khoá).
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import time
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from jobcrawler.config import Settings
from jobcrawler.logging import get_logger
from jobcrawler.resources import Resources, open_resources
from jobcrawler.services import schedule

log = get_logger(__name__)

# Khoá advisory của scheduler (số cố định, khác khoá index trang tuyển dụng).
LEADER_LOCK = 0x6A6F_6273_6368  # "jobsch"
STANDBY_SECONDS = 5.0


async def _try_lead(conn: AsyncConnection) -> bool:
    got = await conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": LEADER_LOCK})
    return bool(got)


async def run_loop(res: Resources, stop: asyncio.Event) -> None:
    s = res.settings
    while not stop.is_set():
        try:
            async with res.engine.connect() as conn:
                while not stop.is_set() and not await _try_lead(conn):
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), STANDBY_SECONDS)
                if stop.is_set():
                    return
                log.info("scheduler.leader")
                while not stop.is_set():
                    started = time.monotonic()
                    try:
                        await schedule.tick(res, datetime.now(UTC))
                    except Exception:
                        log.exception("scheduler.tick_error")
                    # Kết nối giữ khoá còn sống (không thì nhường khoá cho replica khác).
                    await conn.execute(text("SELECT 1"))
                    wait = max(0.0, s.scheduler_tick_seconds - (time.monotonic() - started))
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), wait)
        except Exception:
            log.exception("scheduler.connection_lost")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), STANDBY_SECONDS)


async def run_scheduler(settings: Settings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = (signal.SIGINT, signal.SIGTERM) if hasattr(loop, "add_signal_handler") else ()
    for signum in signals:
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, stop.set)
    async with open_resources(settings) as res:
        log.info(
            "scheduler.start",
            build_window=settings.build_window,
            poll_window=settings.poll_window,
            poll_interval=settings.poll_interval_seconds,
            timezone=settings.crawl_timezone,
        )
        await run_loop(res, stop)
