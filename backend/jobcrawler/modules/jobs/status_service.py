"""Trạng thái hiển thị của việc làm trong truy vấn SQL.

`pipeline/store.job_to_out` trả job `active` đã quá hạn nộp (`expires_at < now`) là `expired`;
lọc, đếm, export "đang mở" / "hết hạn" theo cùng định nghĩa đó để số liệu khớp với từng dòng."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, and_, case, or_

from jobcrawler.common.schemas import JobStatus
from jobcrawler.database.models import Job

ACTIVE = JobStatus.ACTIVE.value
EXPIRED = JobStatus.EXPIRED.value


def open_jobs(now: datetime) -> ColumnElement[bool]:
    """Job đang mở: `active` và chưa quá hạn nộp (không ghi hạn coi như còn mở)."""
    return and_(Job.status == ACTIVE, or_(Job.expires_at.is_(None), Job.expires_at >= now))


def expired_jobs(now: datetime) -> ColumnElement[bool]:
    """Job hết hạn: `expired`, hoặc `active` mà đã quá hạn nộp."""
    return or_(Job.status == EXPIRED, and_(Job.status == ACTIVE, Job.expires_at < now))


def status_filter(status: str, now: datetime) -> ColumnElement[bool] | None:
    """Điều kiện cho bộ lọc trạng thái (`active` | `expired` | `removed`); rỗng / `all`: None."""
    if not status or status == "all":
        return None
    if status == ACTIVE:
        return open_jobs(now)
    if status == EXPIRED:
        return expired_jobs(now)
    return Job.status == status


def shown_status(now: datetime) -> Any:
    """Biểu thức trạng thái như `job_to_out` trả (để GROUP BY)."""
    return case((and_(Job.status == ACTIVE, Job.expires_at < now), EXPIRED), else_=Job.status)
