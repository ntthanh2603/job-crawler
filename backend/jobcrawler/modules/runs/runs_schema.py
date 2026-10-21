"""Mô hình trả về của lượt chạy (build / poll) và trang parse lỗi."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from jobcrawler.common.api import ApiOut, Timestamp
from jobcrawler.common.schemas import RunKind, RunTrigger


class RunOut(ApiOut):
    run_id: str
    company_id: UUID
    kind: RunKind
    trigger: RunTrigger
    # Timestamp: lượt chạy gần đây của trang tổng quan (`RecentRunOut`) đọc từ JSON cache.
    started_at: Timestamp
    finished_at: Timestamp | None
    duration_ms: int | None
    status: str
    jobs_found: int
    jobs_accepted: int
    jobs_new: int
    jobs_closed: int
    incomplete_sources: list[str]
    error: str | None


class FailedPageItem(ApiOut):
    id: UUID
    url: str
    source: str
    reason: str
    created_at: datetime


class FetchOut(ApiOut):
    source: str
    url: str
    http_status: int | None
    outcome: str
    duration_ms: int | None
    items: int | None
    error: str | None


class LogOut(ApiOut):
    ts: datetime
    level: str
    event: str
    context: dict[str, Any]


class SourceCount(ApiOut):
    source: str
    total: int
    outcomes: dict[str, int]


class RunDetailOut(ApiOut):
    run: RunOut
    company_name: str | None
    by_source: list[SourceCount]
    # Request / log lấy theo trang ở /runs/{id}/fetches|logs.
    fetches_total: int
    logs_total: int
    failed_pages: list[FailedPageItem]


class FailedPageOut(FailedPageItem):
    http_status: int | None
    run_id: str | None
    html: str


def run_out(run: Any) -> RunOut:
    return RunOut(
        run_id=str(run.run_id),
        company_id=run.company_id,
        kind=run.kind,
        trigger=run.trigger,
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_ms=run.duration_ms,
        status=run.status,
        jobs_found=run.jobs_found,
        jobs_accepted=run.jobs_accepted,
        jobs_new=run.jobs_new,
        jobs_closed=run.jobs_closed,
        incomplete_sources=list(run.incomplete_sources or []),
        error=run.error,
    )


def failed_page_item(f: Any) -> FailedPageItem:
    return FailedPageItem(
        id=f.id, url=f.url, source=f.source, reason=f.reason, created_at=f.created_at
    )
