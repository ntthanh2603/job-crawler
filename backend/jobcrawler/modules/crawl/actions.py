"""Thao tác thu thập cho API: build / poll ngay, số đo độ trễ phát hiện."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from jobcrawler.common.schemas import RunTrigger
from jobcrawler.modules.crawl import crawl, schedule, sla
from jobcrawler.services.resources import Resources


async def enqueue_build(res: Resources, company_id: UUID) -> bool:
    """Build lại bộ khung một doanh nghiệp ngay (tìm nguồn lại). False nếu đang chờ / chạy."""
    return (
        await crawl.enqueue_builds(res, [company_id], trigger=RunTrigger.MANUAL, refresh=True) > 0
    )


async def enqueue_poll(res: Resources, company_id: UUID) -> bool:
    """Poll một doanh nghiệp ngay theo bộ khung. False nếu đang chờ / chạy."""
    return await crawl.enqueue_polls(res, [company_id], trigger=RunTrigger.MANUAL) > 0


async def crawl_status(res: Resources, *, minutes: int = 60) -> dict[str, object]:
    """Số đo độ trễ phát hiện + độ tươi + khung giờ, cho trang Tổng quan."""
    s = res.settings
    now = datetime.now(UTC)
    stats = await sla.poll_stats(res.redis, s.redis_prefix, minutes=minutes)
    async with res.sessionmaker() as session:
        fresh = await sla.freshness(session, now)
    build_window = schedule.build_window(s)
    poll_window = schedule.poll_window(s)
    return {
        "target_seconds": sla.TARGET_SECONDS,
        "poll_interval_seconds": s.poll_interval_seconds,
        "polls": stats.as_dict(),
        "freshness": fresh.as_dict(),
        "windows": {
            "timezone": s.crawl_timezone,
            "build": s.build_window,
            "poll": s.poll_window,
            "in_build": build_window.contains(now),
            "in_poll": poll_window.contains(now),
            "next_build_start": build_window.next_start(now),
            "next_poll_start": poll_window.next_start(now),
        },
    }
