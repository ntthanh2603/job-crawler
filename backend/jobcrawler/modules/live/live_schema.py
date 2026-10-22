"""Mô hình sự kiện của `GET /api/events` (`modules/live/live_service.py`)."""

from __future__ import annotations

from typing import Literal

from jobcrawler.common.api import ApiOut
from jobcrawler.modules.gateway.gateway_schema import WorkersPageOut
from jobcrawler.modules.queue.queue_schema import QueuePageOut
from jobcrawler.modules.runs.runs_schema import RunDetailOut

LiveTopic = Literal["workers", "queue", "run"]
TOPICS: tuple[LiveTopic, ...] = ("workers", "queue", "run")


class LiveOut(ApiOut):
    """Một sự kiện của `GET /api/events`: trường tên `topic` có dữ liệu (cùng mô hình với
    `GET /api/workers`, `GET /api/queue`, `GET /api/runs/{run_id}`), hai trường còn lại null.
    `run` null khi không có lượt build / poll này."""

    topic: LiveTopic
    workers: WorkersPageOut | None = None
    queue: QueuePageOut | None = None
    run: RunDetailOut | None = None
