"""Mô hình trả về của API trang Workers."""

from __future__ import annotations

from typing import Literal

from jobcrawler.common.api import ApiOut
from jobcrawler.modules.gateway.registry import WorkerStatus


class WorkerPoolOut(ApiOut):
    """Cộng dồn các worker đang sống của một pool."""

    workers: int
    http_slots: int
    render_slots: int
    http_inflight: int
    render_inflight: int


class WorkersSummaryOut(ApiOut):
    alive: int
    gone: int
    draining: int
    pools: dict[str, WorkerPoolOut]
    http_slots: int
    render_slots: int
    http_inflight: int
    render_inflight: int
    done: int  # cả worker vừa rời
    failed: int


class WorkersPageOut(ApiOut):
    workers: list[WorkerStatus]  # đang sống trước, kèm worker vừa rời
    summary: WorkersSummaryOut
    now_ms: int  # giờ máy chủ (ms epoch) lúc trả lời, để tính "lần cuối báo" không lệch đồng hồ
    # Gateway hỏi khoá khi worker nối vào (GATEWAY_TOKEN); False = worker nào cũng vào được.
    auth_enabled: bool


class JoinKeyOut(ApiOut):
    """Khoá tham gia dùng một lần cho một máy worker mới (chỉ trả `key` lúc tạo)."""

    id: str
    key: str
    expires_at_ms: int
    ttl_seconds: int


class JoinKeyStatusOut(ApiOut):
    id: str
    status: Literal["pending", "used", "expired"]
    worker_id: str | None  # worker đã dùng khoá (khi `used`)


class DrainOut(ApiOut):
    worker_id: str
    draining: bool
