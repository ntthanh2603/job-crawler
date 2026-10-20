"""Thao tác bảo trì cho API: xếp cron bảo trì vào queue."""

from __future__ import annotations

from jobcrawler.modules.maintenance.maintenance import LOCK_KEY
from jobcrawler.services.resources import Resources


async def enqueue_maintenance(res: Resources) -> bool:
    """Chạy cron bảo trì ngay (ở runner vai `build`)."""
    job = await res.queues.build.enqueue("maintenance_cron", key=f"{LOCK_KEY}:manual", timeout=3600)
    return job is not None
