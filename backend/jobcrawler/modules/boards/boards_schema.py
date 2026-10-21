"""Mô hình trả về của API trang tuyển dụng."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from jobcrawler.common.api import ApiOut, QueuedOut, known_values
from jobcrawler.modules.overview.overview_schema import BoardStatsOut, QueueStatsOut


class BoardRunOut(ApiOut):
    id: UUID
    source: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    complete: bool
    urls_seen: int
    fetched: int
    new: int
    updated: int
    unchanged: int
    failed: int
    removed: int
    profiles_new: int
    profiles_matched: int
    error: str | None
    # full = index đủ (đêm), tail = index đuôi (ngày, chỉ tin mới nhất).
    mode: Annotated[str, known_values(("full", "tail"))]


class BoardsOut(ApiOut):
    boards: list[BoardStatsOut]
    queue: QueueStatsOut


class BoardIndexOut(QueuedOut):
    workers: int  # runner đang nghe queue trang tuyển dụng
