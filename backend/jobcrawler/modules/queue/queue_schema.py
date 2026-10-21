"""Mô hình trả về của trang Hàng đợi."""

from __future__ import annotations

from typing import Annotated

from jobcrawler.common.api import ApiOut, known_values
from jobcrawler.common.schemas import RunTrigger
from jobcrawler.modules.overview.overview_schema import QueueStatsOut


class QueueJobOut(ApiOut):
    key: str
    function: str
    status: str
    attempts: int
    retries: int
    timeout: int  # giây, 0 = không giới hạn
    queued: int  # ms epoch
    started: int  # ms epoch
    # Hẹn giờ chạy (giây epoch, 0 = chạy ngay); lượt tự chạy tiếp hẹn sau ~1 phút.
    scheduled: int
    error: str | None
    label: str
    company_id: str | None
    # Vì sao job được xếp (build đêm, poll theo chu kỳ, poll lỗi → build lại...).
    trigger: Annotated[str | None, known_values(RunTrigger, nullable=True)]
    # Lượt chạy tiếp thứ mấy sau lượt `partial` (0 = lượt thường).
    continuation: int


class QueueOut(QueueStatsOut):
    jobs: list[QueueJobOut]  # một trang (QUEUE_PAGE job): đang chạy → đang chờ → hẹn giờ
    page: int
    pages: int
    total: int


class QueuePageOut(ApiOut):
    queues: list[QueueOut]
