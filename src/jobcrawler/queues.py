"""Hàng đợi job: Redis Streams (`streamqueue`, mặc định) hoặc SAQ (QUEUE_BACKEND=saq, để quay lại).

Hai loại dùng chung cách gọi: `enqueue(function, key=…, timeout=…, retries=…, scheduled=…,
**tham số)` trả job hoặc None (khoá đang có job chờ/chạy), `info(jobs=…)` trả số đếm + worker +
một trang job. Tham số và kết quả job ghi JSON; id (UUID), datetime... ghi dạng chuỗi.
"""

from __future__ import annotations

from typing import Any, Literal

from redis.asyncio import Redis
from saq import Queue

from jobcrawler.config import Settings
from jobcrawler.streamqueue import StreamQueue, dump_json

type JobQueue = Queue | StreamQueue
Backend = Literal["streams", "saq"]
_PATTERNS: dict[Backend, tuple[str, ...]] = {
    "saq": ("saq:{name}:*", "saq:job:{name}:*"),
    "streams": ("sq:{name}:*",),
}


def queue_from_url(redis_url: str, name: str) -> Queue:
    """Queue SAQ (QUEUE_BACKEND=saq)."""
    return Queue.from_url(redis_url, name=name, dump=dump_json)


def make_queue(settings: Settings, name: str) -> JobQueue:
    if settings.queue_backend == "saq":
        return queue_from_url(settings.redis_url, name)
    return StreamQueue(settings.redis_url, name, reclaim_seconds=settings.queue_reclaim_seconds)


async def drop_queue(
    redis_url: str, name: str, backends: tuple[Backend, ...] = ("streams", "saq")
) -> int:
    """Xoá mọi khoá của queue `name` (job, thống kê, stream) ở các loại `backends`. Trả số khoá
    đã xoá."""
    redis = Redis.from_url(redis_url)
    deleted = 0
    try:
        for pattern in (p.format(name=name) for b in backends for p in _PATTERNS[b]):
            keys = [k async for k in redis.scan_iter(match=pattern, count=1000)]
            for i in range(0, len(keys), 1000):
                deleted += await redis.delete(*keys[i : i + 1000])
    finally:
        await redis.aclose()
    return deleted


async def pending_jobs(queue: JobQueue) -> list[dict[str, Any]]:
    """Mọi job đang chờ / hẹn giờ / chạy của queue (dạng `info(jobs=True)`)."""
    info = await queue.info(jobs=True, offset=0, limit=1_000_000)
    return list(info.get("jobs") or [])


async def move_jobs(source: JobQueue, target: JobQueue) -> tuple[int, int]:
    """Chép job chưa xong từ `source` sang `target` (cùng khoá, tham số, timeout, lượt chạy, giờ
    hẹn), bỏ job cron (worker của `target` tự xếp lại). Trả (đã chép, đã có ở `target`).

    Chỉ chạy khi worker của cả hai loại đã dừng; sau đó xoá `source` bằng `drop_queue`."""
    moved = existed = 0
    for job in await pending_jobs(source):
        key = str(job.get("key") or "")
        if key.startswith("cron:"):
            continue
        created = await target.enqueue(
            job["function"],
            key=key,
            # `saq.Job.to_dict` bỏ trường bằng giá trị mặc định (timeout 10 s, 1 lượt chạy).
            timeout=job.get("timeout", 10),
            retries=job.get("retries") or 1,
            scheduled=job.get("scheduled") or 0,
            **(job.get("kwargs") or {}),
        )
        if created is None:
            existed += 1
        else:
            moved += 1
    return moved, existed
