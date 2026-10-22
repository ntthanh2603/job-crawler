"""Relay sự kiện việc làm (docs/ARCHITECTURE.md mục 12): chạy trong process `scheduler`, song song
vòng xếp lịch, một bản trên cụm (lease Redis `<prefix>:lock:relay`, khác khoá leader của
scheduler để nhịp xếp lịch / kiểm cảnh báo không làm chậm feed).

Mỗi `RELAY_INTERVAL_SECONDS`: gán seq cho sự kiện đã commit (`feed_service.sequence`, tới khi hết)
và ghi seq mới nhất lên Redis chính (`feed_service.head_key`) cho feed long-poll."""

from __future__ import annotations

import asyncio
import contextlib

from jobcrawler.common.logging import get_logger
from jobcrawler.common.watchdog import Watchdog
from jobcrawler.modules.integration import feed_service
from jobcrawler.services.redis.locks import RedisLease
from jobcrawler.services.resources import Resources

log = get_logger(__name__)

LEASE_SECONDS = 30.0
STANDBY_SECONDS = 5.0
PART = "relay"  # phần của watchdog process scheduler


def lease_key(prefix: str) -> str:
    return f"{prefix}:lock:relay"


async def sequence_pending(res: Resources) -> int:
    """Gán seq cho mọi sự kiện đã commit (lô `SEQUENCE_BATCH`, mỗi lô một transaction), ghi head
    lên Redis. Trả seq mới nhất."""
    total = 0
    while True:
        async with res.sessionmaker.begin() as session:
            assigned, last = await feed_service.sequence(session)
        total += assigned
        if assigned < feed_service.SEQUENCE_BATCH:
            break
    await res.redis.set(feed_service.head_key(res.settings.redis_prefix), last)
    if total:
        log.debug("relay.sequenced", events=total, head=last)
    return last


async def run_relay(res: Resources, stop: asyncio.Event, watchdog: Watchdog | None = None) -> None:
    lease = RedisLease(res.redis, lease_key(res.settings.redis_prefix), ttl=LEASE_SECONDS)
    leading = False
    try:
        while not stop.is_set():
            if watchdog is not None:
                watchdog.loop_iteration(PART)
            wait = res.settings.relay_interval_seconds
            try:
                if not await lease.acquire():
                    if leading:
                        log.warning("relay.lease_lost")
                        leading = False
                    wait = STANDBY_SECONDS
                else:
                    if not leading:
                        log.info("relay.leader", lease=lease.token)
                        leading = True
                    await sequence_pending(res)
            except Exception:
                log.exception("relay.error")
                wait = STANDBY_SECONDS
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), wait)
    finally:
        if leading:
            with contextlib.suppress(Exception):
                await lease.release()
