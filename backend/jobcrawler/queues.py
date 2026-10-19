"""Hàng đợi job trên Redis Streams (`streamqueue`): build, poll, boards.

`enqueue(function, key=…, timeout=…, retries=…, scheduled=…, **tham số)` trả job hoặc None (khoá
đang có job chờ/chạy), `info(jobs=…)` trả số đếm + runner + một trang job. Tham số và kết quả job
ghi JSON; id (UUID), datetime... ghi dạng chuỗi.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis

from jobcrawler.config import Settings
from jobcrawler.streamqueue import StreamQueue

type JobQueue = StreamQueue


def make_queue(settings: Settings, name: str) -> StreamQueue:
    return StreamQueue(settings.redis_url, name, reclaim_seconds=settings.queue_reclaim_seconds)


@dataclass(frozen=True, slots=True)
class Queues:
    """Ba hàng đợi của hệ thống (xem Settings.*_queue_name)."""

    build: StreamQueue  # build bộ khung (đêm, khi poll lỗi, khi người dùng chạy)
    poll: StreamQueue  # poll tin mới theo bộ khung (ngày)
    boards: StreamQueue  # index trang tuyển dụng (đủ ban đêm, đuôi ban ngày)

    @classmethod
    def from_settings(cls, settings: Settings) -> Queues:
        build, poll, boards = settings.queue_names
        return cls(
            build=make_queue(settings, build),
            poll=make_queue(settings, poll),
            boards=make_queue(settings, boards),
        )

    def all(self) -> tuple[StreamQueue, StreamQueue, StreamQueue]:
        return self.build, self.poll, self.boards

    async def connect(self) -> None:
        for queue in self.all():
            await queue.connect()

    async def disconnect(self) -> None:
        for queue in self.all():
            await queue.disconnect()


async def drop_queue(redis_url: str, name: str) -> int:
    """Xoá mọi khoá của queue `name` (job, thống kê, stream). Trả số khoá đã xoá."""
    redis = Redis.from_url(redis_url)
    deleted = 0
    try:
        keys = [k async for k in redis.scan_iter(match=f"sq:{name}:*", count=1000)]
        for i in range(0, len(keys), 1000):
            deleted += await redis.delete(*keys[i : i + 1000])
    finally:
        await redis.aclose()
    return deleted


async def pending_jobs(queue: JobQueue) -> list[dict[str, Any]]:
    """Mọi job đang chờ / hẹn giờ / chạy của queue (dạng `info(jobs=True)`)."""
    info = await queue.info(jobs=True, offset=0, limit=1_000_000)
    return list(info.get("jobs") or [])
