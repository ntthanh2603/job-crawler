"""Dựng `FetchResult` (protobuf) dùng chung cho HTTP và render: hạn chờ, lỗi, nén body."""

from __future__ import annotations

import asyncio
import time

import zstandard

from jcproto.v1 import crawl_pb2

# Việc không ghi `timeout_seconds` (0): chờ tối đa chừng này giây.
DEFAULT_TIMEOUT_SECONDS = 30.0
# Body lớn hơn chừng này thì nén zstd (một frame); nén ở thread phụ khi lớn hơn OFFLOAD_BYTES để
# không chặn event loop.
COMPRESS_MIN_BYTES = 2048
OFFLOAD_BYTES = 512 * 1024
ZSTD_LEVEL = 3

_compressor = zstandard.ZstdCompressor(level=ZSTD_LEVEL)


def now_ms() -> int:
    return int(time.time() * 1000)


def expired(task: crawl_pb2.FetchTask) -> bool:
    """Quá `deadline_ms`: backend đã thôi chờ, không tải nữa."""
    return task.deadline_ms > 0 and now_ms() >= task.deadline_ms


def task_timeout(task: crawl_pb2.FetchTask) -> float:
    """Giây chờ tối đa: min(`timeout_seconds`, thời gian còn lại tới `deadline_ms`); ≤ 0 = hết."""
    timeout = task.timeout_seconds if task.timeout_seconds > 0 else DEFAULT_TIMEOUT_SECONDS
    if task.deadline_ms > 0:
        timeout = min(timeout, (task.deadline_ms - now_ms()) / 1000)
    return timeout


def failure(
    task: crawl_pb2.FetchTask,
    kind: crawl_pb2.ErrorKind,
    error: str,
    *,
    worker_id: str,
    proxy_id: str = "",
    duration_ms: int = 0,
    status: int = 0,
) -> crawl_pb2.FetchResult:
    return crawl_pb2.FetchResult(
        task_id=task.task_id,
        status=status,
        final_url=task.url,
        error_kind=kind,
        error=error[:2000],
        proxy_id=proxy_id,
        duration_ms=duration_ms,
        worker_id=worker_id,
    )


def _compress_new(body: bytes) -> bytes:
    # ZstdCompressor không an toàn đa luồng: thread phụ dùng bản riêng.
    return zstandard.ZstdCompressor(level=ZSTD_LEVEL).compress(body)


async def encode_body(body: bytes) -> tuple[bytes, bool]:
    """Body → (bytes gửi đi, đã nén zstd chưa)."""
    if len(body) <= COMPRESS_MIN_BYTES:
        return body, False
    if len(body) > OFFLOAD_BYTES:
        return await asyncio.to_thread(_compress_new, body), True
    return _compressor.compress(body), True


def elapsed_ms(started: float) -> int:
    """`started`: giá trị `time.monotonic()` lúc bắt đầu."""
    return int((time.monotonic() - started) * 1000)
