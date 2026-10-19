"""So hai loại hàng đợi (SAQ, Redis Streams) trên Redis thật, chỉ đo phần hàng đợi (job giả).

uv run python scripts/loadtest/queue_bench.py [--out kết_quả.json]

Mỗi loại: 3 process worker × 10 slot (như compose), lần lượt:
1. xếp 10.000 job (tốc độ enqueue, 50 lệnh song song như `batch.enqueue_companies`);
2. 3.000 job rỗng: thông lượng tối đa, độ trễ từ lúc xếp tới lúc chạy;
3. 600 job 0,5 s trên 30 slot: thời gian thực so với lý tưởng 10 s (phần hao của hàng đợi);
4. 30 job 20 s chiếm hết slot, giết hẳn một worker (TerminateProcess / SIGKILL): bao lâu sau 10 job
   của nó chạy lại xong ở worker khác (timeout job 180 s, chạy tối đa 2 lần, như process_company).
Queue tên `bench-…`, xoá khi xong.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from saq import Worker

from jobcrawler.config import get_settings
from jobcrawler.queues import JobQueue, drop_queue, make_queue
from jobcrawler.streamqueue import StreamQueue, StreamWorker

WORKERS = 3
CONCURRENCY = 10


async def sleep_job(ctx: Any, *, seconds: float = 0.0) -> None:
    if seconds:
        await asyncio.sleep(seconds)


def _queue(backend: str, name: str) -> JobQueue:
    settings = get_settings().model_copy(update={"queue_backend": backend})
    return make_queue(settings, name)


async def run_worker(backend: str, name: str) -> None:
    queue = _queue(backend, name)
    await queue.connect()
    w: Any
    if isinstance(queue, StreamQueue):
        w = StreamWorker(queue, functions=[sleep_job], concurrency=CONCURRENCY)
    else:
        w = Worker(queue, functions=[sleep_job], concurrency=CONCURRENCY)
    await w.start()


def spawn(backend: str, name: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, __file__, "worker", backend, name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


async def wait_for(check: Any, limit: float) -> float:
    started = time.monotonic()
    while not await check():
        if time.monotonic() - started > limit:
            raise TimeoutError(f"quá {limit:g} s")
        await asyncio.sleep(0.1)
    return time.monotonic() - started


async def enqueue_many(queue: JobQueue, prefix: str, n: int, **kwargs: Any) -> float:
    sem = asyncio.Semaphore(50)

    async def one(i: int) -> None:
        async with sem:
            await queue.enqueue("sleep_job", key=f"{prefix}{i}", **kwargs)

    started = time.monotonic()
    await asyncio.gather(*(one(i) for i in range(n)))
    return time.monotonic() - started


async def jobs_done(queue: JobQueue, keys: list[str]) -> list[Any]:
    out = []
    for key in keys:
        job = await queue.job(key)
        out.append(job)
    return out


async def all_complete(queue: JobQueue, keys: list[str]) -> bool:
    for key in reversed(keys):  # job cuối xong muộn nhất: hỏng sớm
        job = await queue.job(key)
        if job is None or str(getattr(job.status, "value", job.status)) != "complete":
            return False
    return True


def span_s(jobs: list[Any]) -> float:
    """Giây từ lúc xếp job đầu tiên tới lúc job cuối cùng xong (theo dấu thời gian của job)."""
    done = [j for j in jobs if j is not None]
    return (max(j.completed for j in done) - min(j.queued for j in done)) / 1000


def waits(jobs: list[Any]) -> list[float]:
    """Giây từ lúc job sẵn sàng (xếp hàng / hẹn) tới lúc bắt đầu chạy lần cuối."""
    return [
        (j.started - max(j.queued, (j.scheduled or 0) * 1000)) / 1000
        for j in jobs
        if j is not None and j.started
    ]


async def bench(backend: str) -> dict[str, Any]:
    name = f"bench-{backend}-{uuid.uuid4().hex[:6]}"
    url = get_settings().redis_url
    queue = _queue(backend, name)
    await queue.connect()
    out: dict[str, Any] = {"backend": backend}
    procs: list[subprocess.Popen[bytes]] = []
    try:
        # 1. enqueue 10.000 (chưa có worker)
        out["enqueue_10k_s"] = round(await enqueue_many(queue, "e", 10_000), 2)
        await queue.disconnect()
        await drop_queue(url, name)
        await queue.connect()

        procs = [spawn(backend, name) for _ in range(WORKERS)]

        async def ready() -> bool:
            return len((await queue.info())["workers"]) >= WORKERS

        await wait_for(ready, 60)
        await asyncio.sleep(2)

        # 2. thông lượng job rỗng
        keys = [f"n{i}" for i in range(3000)]
        await enqueue_many(queue, "n", 3000, timeout=60, retries=2)
        await wait_for(lambda: all_complete(queue, keys), 300)
        jobs = await jobs_done(queue, keys)
        span = span_s(jobs)
        w = waits(jobs)
        out["noop_3000"] = {
            "seconds": round(span, 2),
            "jobs_per_s": round(3000 / span, 1),
            "wait_p50_s": round(pct(w, 0.5), 3),
            "wait_p95_s": round(pct(w, 0.95), 3),
        }

        # 3. 600 job 0,5 s trên 30 slot
        keys = [f"h{i}" for i in range(600)]
        await enqueue_many(queue, "h", 600, timeout=60, retries=2, seconds=0.5)
        await wait_for(lambda: all_complete(queue, keys), 300)
        jobs = await jobs_done(queue, keys)
        span = span_s(jobs)
        ideal = 600 * 0.5 / (WORKERS * CONCURRENCY)
        w = waits(jobs)
        out["half_second_600"] = {
            "seconds": round(span, 2),
            "ideal_s": ideal,
            "overhead_pct": round((span / ideal - 1) * 100, 1),
            "wait_p50_s": round(pct(w, 0.5), 2),
            "wait_p95_s": round(pct(w, 0.95), 2),
        }

        # 4. giết một worker đang giữ 10 job
        keys = [f"k{i}" for i in range(30)]
        await enqueue_many(queue, "k", 30, timeout=180, retries=2, seconds=20)

        async def full() -> bool:
            return (await queue.info())["active"] >= 30

        await wait_for(full, 30)
        procs[0].kill()
        killed = time.time()
        await wait_for(lambda: all_complete(queue, keys), 600)
        jobs = await jobs_done(queue, keys)
        recovered = max(j.completed for j in jobs if j is not None) / 1000 - killed
        rerun = [j for j in jobs if j is not None and j.attempts > 1]
        out["kill_worker"] = {
            "jobs_rerun": len(rerun),
            "all_done_after_kill_s": round(recovered, 1),
            # trừ 20 s chạy lại: thời gian job mồ côi nằm chờ trước khi được thu hồi
            "detect_s": round(recovered - 20, 1),
        }
    finally:
        for p in procs:
            p.kill()
        for p in procs:
            p.wait(10)
        await queue.disconnect()
        await drop_queue(url, name)
    return out


async def main() -> list[dict[str, Any]]:
    return [await bench(b) for b in ("saq", "streams")]


def report(results: list[dict[str, Any]], out_path: Path | None) -> None:
    sys.stdout.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]
    text = json.dumps(results, indent=2, ensure_ascii=False)
    print(text)
    if out_path:
        out_path.write_text(text, encoding="utf-8")
    for r in results:
        noop, half, kill = r["noop_3000"], r["half_second_600"], r["kill_worker"]
        print(
            f"{r['backend']:<8} enqueue 10k {r['enqueue_10k_s']} s · rỗng {noop['jobs_per_s']}/s"
            f" (chờ p95 {noop['wait_p95_s']} s) · 600×0,5 s {half['seconds']} s"
            f" (+{half['overhead_pct']}%) · giết worker: xong sau {kill['all_done_after_kill_s']} s"
        )


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "worker":
        asyncio.run(run_worker(sys.argv[2], sys.argv[3]))
    else:
        parser = argparse.ArgumentParser()
        parser.add_argument("--out", type=Path)
        report(asyncio.run(main()), parser.parse_args().out)
