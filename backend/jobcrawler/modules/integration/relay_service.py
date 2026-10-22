"""Relay sự kiện việc làm (docs/ARCHITECTURE.md mục 12): chạy trong process `scheduler`, song song
vòng xếp lịch, một bản trên cụm (lease Redis `<prefix>:lock:relay`, khác khoá leader của
scheduler để nhịp xếp lịch / kiểm cảnh báo không làm chậm feed).

Mỗi `RELAY_INTERVAL_SECONDS`: gán seq cho sự kiện đã commit (`feed_service.sequence`, tới khi hết)
và ghi seq mới nhất lên Redis chính (`feed_service.head_key`) cho feed long-poll. Mỗi client bật
webhook một task giao riêng (`webhook_service.deliver`): có seq mới thì được đánh thức, lỗi thì lùi
1 s → 5 phút, client chậm / lỗi không làm chậm client khác. Danh sách client đọc lại mỗi
`TARGETS_SECONDS`; mất lease thì dừng mọi task giao (relay khác thay)."""

from __future__ import annotations

import asyncio
import contextlib
import time
from uuid import UUID

from jobcrawler.common.logging import get_logger
from jobcrawler.common.watchdog import Watchdog
from jobcrawler.modules.integration import feed_service, webhook_service
from jobcrawler.services.redis.locks import RedisLease
from jobcrawler.services.resources import Resources

log = get_logger(__name__)

LEASE_SECONDS = 30.0
STANDBY_SECONDS = 5.0
TARGETS_SECONDS = 10.0
# Task giao không được đánh thức thì tự kiểm lại sau chừng này giây.
IDLE_SECONDS = 5.0
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


async def _deliver_forever(
    res: Resources, target: webhook_service.Target, wake: asyncio.Event
) -> None:
    failures = 0
    while True:
        wake.clear()
        try:
            done = await webhook_service.deliver(res, target)
        except Exception as exc:
            log.warning("webhook.error", client=target.name, error=f"{type(exc).__name__}: {exc}")
            done = webhook_service.Delivery(error=str(exc))
        if done.error is not None:
            failures += 1
            await asyncio.sleep(webhook_service.backoff(failures))
            continue
        failures = 0
        if done.more:
            continue
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(wake.wait(), IDLE_SECONDS)


class Dispatchers:
    """Task giao webhook của từng client, theo cấu hình đọc từ DB."""

    def __init__(self, res: Resources) -> None:
        self.res = res
        self.tasks: dict[UUID, asyncio.Task[None]] = {}
        self.targets: dict[UUID, webhook_service.Target] = {}
        self.wakes: dict[UUID, asyncio.Event] = {}
        self.synced = 0.0  # time.monotonic() của lần đọc danh sách client gần nhất

    async def sync(self, *, force: bool = False) -> None:
        if not force and time.monotonic() - self.synced < TARGETS_SECONDS:
            return
        self.synced = time.monotonic()
        targets = {t.client_id: t for t in await webhook_service.targets(self.res)}
        for client_id in list(self.tasks):
            if targets.get(client_id) != self.targets.get(client_id):
                await self._stop(client_id)
        for client_id, target in targets.items():
            if client_id not in self.tasks:
                wake = asyncio.Event()
                self.wakes[client_id] = wake
                self.tasks[client_id] = asyncio.create_task(
                    _deliver_forever(self.res, target, wake), name=f"webhook:{target.name}"
                )
                log.info("webhook.started", client=target.name)
        self.targets = targets

    def notify(self) -> None:
        for wake in self.wakes.values():
            wake.set()

    async def _stop(self, client_id: UUID) -> None:
        task = self.tasks.pop(client_id)
        self.wakes.pop(client_id, None)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def close(self) -> None:
        for client_id in list(self.tasks):
            await self._stop(client_id)
        self.targets = {}
        self.synced = 0.0


async def run_relay(res: Resources, stop: asyncio.Event, watchdog: Watchdog | None = None) -> None:
    lease = RedisLease(res.redis, lease_key(res.settings.redis_prefix), ttl=LEASE_SECONDS)
    dispatchers = Dispatchers(res)
    leading = False
    head = -1
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
                        await dispatchers.close()
                    wait = STANDBY_SECONDS
                else:
                    if not leading:
                        log.info("relay.leader", lease=lease.token)
                        leading = True
                    last = await sequence_pending(res)
                    await dispatchers.sync()
                    if last != head:
                        head = last
                        dispatchers.notify()
            except Exception:
                log.exception("relay.error")
                wait = STANDBY_SECONDS
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), wait)
    finally:
        await dispatchers.close()
        if leading:
            with contextlib.suppress(Exception):
                await lease.release()
