"""Scheduler: xếp build đêm và poll ngày (modules/crawl/schedule_service.py) mỗi
SCHEDULER_TICK_SECONDS. Cùng nhịp đó, leader ghi các dòng nhật ký thao tác process không nối
Postgres để lại trên Redis (gateway: `worker.joined`, `audit_service.flush_pending`) vào bảng, và
mỗi ALERT_CHECK_SECONDS kiểm các dấu hiệu chậm / lỗi để gửi cảnh báo (`alert_service.check`).

Chạy nhiều replica được: chỉ replica giữ khoá leader (lease trên Redis chính,
`jobcrawler.services.redis.locks`, khoá `<prefix>:lock:scheduler`, hạn LEASE_SECONDS, gia hạn mỗi
nhịp) làm việc; các replica khác thử lại mỗi STANDBY_SECONDS và thay khi leader chết (khoá hết hạn)
hoặc dừng (nhả khoá). Không giữ kết nối Postgres suốt đời process nên chạy được sau PgBouncer chế độ
transaction. Leader treo quá hạn khoá thì mất khoá (lần gia hạn sau báo `scheduler.lease_lost`) —
nhịp xếp lịch chạy trùng trong chốc lát vô hại vì job có khoá duy nhất.

Song song vòng xếp lịch là relay sự kiện việc làm cho hệ thống ngoài
(`modules/integration/relay_service.py`, lease riêng `<prefix>:lock:relay`).

Health: vòng lặp chính ghi heartbeat `scheduler` (`jobcrawler.services.redis.heartbeat`) — replica
dự phòng sau mỗi lần hỏi được Postgres, replica làm việc sau mỗi nhịp xếp lịch thành công; mất
Postgres / Redis / nhịp lỗi liên tục thì heartbeat cũ dần và `jobcrawler health scheduler` báo hỏng.

Watchdog (`jobcrawler.common.watchdog`, WATCHDOG_SECONDS): mọi vòng của vòng lặp chính (kể cả vòng
lỗi, chờ kết nối lại) gọi `loop_iteration()`; event loop hoặc vòng lặp đứng quá ngưỡng thì process
thoát (mã 70) để Docker dựng lại. Mất Postgres không làm thoát.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import time
from datetime import UTC, datetime

from sqlalchemy import text

from jobcrawler.common.config import Settings
from jobcrawler.common.logging import get_logger
from jobcrawler.common.watchdog import Watchdog, start_watchdog
from jobcrawler.modules.alerts import alert_service
from jobcrawler.modules.audit import audit_service
from jobcrawler.modules.crawl import schedule_service
from jobcrawler.modules.integration import relay_service
from jobcrawler.services.redis.heartbeat import Heartbeat
from jobcrawler.services.redis.locks import RedisLease
from jobcrawler.services.resources import Resources, open_resources

log = get_logger(__name__)

STANDBY_SECONDS = 5.0
# Hạn khoá leader: leader gia hạn mỗi nhịp (SCHEDULER_TICK_SECONDS); chết thì replica khác thay sau
# tối đa chừng này giây.
LEASE_SECONDS = 30.0
COMPONENT = "scheduler"
# Một lượt kiểm cảnh báo không được giữ nhịp xếp lịch lâu hơn chừng này giây.
ALERT_TIMEOUT_SECONDS = 15.0


def leader_key(prefix: str) -> str:
    return f"{prefix}:lock:scheduler"


async def _db_ok(res: Resources) -> None:
    """Replica dự phòng: hỏi được Postgres mới coi là khoẻ (ném lỗi khi không)."""
    async with res.engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def _flush_audit(res: Resources) -> None:
    """Dòng nhật ký chờ trên Redis → `audit_events`; lỗi thì dòng ở lại hàng chờ, nhịp sau ghi."""
    try:
        await audit_service.flush_pending(res.redis, res.settings.redis_prefix, res.sessionmaker)
    except Exception as exc:
        log.warning("scheduler.audit_flush_failed", error=f"{type(exc).__name__}: {exc}")


async def _check_alerts(res: Resources) -> None:
    """Một lượt kiểm cảnh báo; lỗi / quá giờ chỉ log, không làm hỏng nhịp xếp lịch."""
    try:
        await asyncio.wait_for(alert_service.check(res, datetime.now(UTC)), ALERT_TIMEOUT_SECONDS)
    except Exception as exc:
        log.warning("scheduler.alerts_failed", error=f"{type(exc).__name__}: {exc}")


async def run_loop(
    res: Resources,
    stop: asyncio.Event,
    beat: Heartbeat | None = None,
    watchdog: Watchdog | None = None,
) -> None:
    s = res.settings
    beat = beat or Heartbeat(res.redis, s.redis_prefix, COMPONENT)
    # Vòng nào cũng báo watchdog (kể cả vòng lỗi): chỉ vòng lặp đứng hẳn mới làm process thoát.
    dog = watchdog or Watchdog(COMPONENT, max_stall=0)
    lease = RedisLease(res.redis, leader_key(s.redis_prefix), ttl=LEASE_SECONDS)
    leading = False
    next_alerts = 0.0  # time.monotonic() của lượt kiểm cảnh báo kế tiếp
    try:
        while not stop.is_set():
            dog.loop_iteration()
            try:
                if not await lease.acquire():
                    if leading:
                        log.warning("scheduler.lease_lost")
                        leading = False
                    await _db_ok(res)
                    await beat.tick()  # dự phòng: còn hỏi được Redis + Postgres là khoẻ
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), STANDBY_SECONDS)
                    continue
                if not leading:
                    log.info("scheduler.leader", lease=lease.token)
                    leading = True
                started = time.monotonic()
                try:
                    await schedule_service.tick(res, datetime.now(UTC))
                except Exception:
                    log.exception("scheduler.tick_error")
                else:
                    await beat.tick()
                await _flush_audit(res)
                if time.monotonic() >= next_alerts:
                    next_alerts = time.monotonic() + res.settings.alert_check_seconds
                    await _check_alerts(res)
                wait = max(0.0, s.scheduler_tick_seconds - (time.monotonic() - started))
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), wait)
            except Exception:
                log.exception("scheduler.connection_lost")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), STANDBY_SECONDS)
    finally:
        if leading:
            # Dừng có chủ đích: nhả khoá để replica khác thay ngay, không chờ hết hạn.
            with contextlib.suppress(Exception):
                await lease.release()


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
        dog = start_watchdog(
            COMPONENT, max_stall=settings.watchdog_seconds, parts=("main", relay_service.PART)
        )
        try:
            await asyncio.gather(
                run_loop(res, stop, watchdog=dog), relay_service.run_relay(res, stop, watchdog=dog)
            )
        finally:
            dog.stop()  # trước khi đóng tài nguyên: lượt dừng chậm không bị watchdog giết
