"""Mô hình trả về của API thu thập (bộ khung, lịch, độ trễ phát hiện)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from jobcrawler.common.api import ApiOut
from jobcrawler.common.schemas import PlanStatus, ScheduleState


class PlanOut(ApiOut):
    id: UUID
    source: str
    kind: str
    url: str
    status: PlanStatus
    jobs: int
    built_at: datetime
    polled_at: datetime | None
    changed_at: datetime | None
    failures: int
    last_error: str | None
    listings: list[str]
    hubs: int
    apis: list[str]
    rendered: int
    llm_listing: bool
    ignored: int
    # Số trang (danh sách, danh mục, API) một lượt poll được đọc ở nguồn này (0 = bộ khung cũ,
    # dùng PLAN_MAX_PAGES) và số trang cần để đọc trọn nguồn.
    pages: int
    pages_needed: int


class ScheduleOut(ApiOut):
    state: ScheduleState
    plans: int
    next_build_at: datetime | None
    next_poll_at: datetime | None
    last_build_at: datetime | None
    last_build_status: str | None
    last_poll_at: datetime | None
    last_poll_ok_at: datetime | None
    last_poll_ms: int | None
    last_change_at: datetime | None
    poll_failures: int
    polls: int
    last_error: str | None


class CompanyCrawlOut(ApiOut):
    schedule: ScheduleOut | None
    plans: list[PlanOut]
    poll_interval_seconds: float


class PollStatsOut(ApiOut):
    """Các lượt poll trong `minutes` phút gần nhất; thời gian tính bằng giây."""

    # Trường của `sla.PollStats` (tests/test_openapi.py kiểm khớp).
    minutes: int
    polls: int
    ok: int
    failed: int
    broken: int
    skipped: int
    changed: int
    new_jobs: int
    per_second: float
    duration_p50: float | None
    duration_p95: float | None
    window_p50: float | None
    window_p95: float | None
    within_target: float | None  # tỉ lệ lượt có cửa sổ phát hiện ≤ mục tiêu (0..1)
    # Số lượt theo ngưỡng cửa sổ (giây): 30, 60, 90, 120, 135, 150, 165, 180, 210, 240, 300, 600,
    # 1800, 3600, +∞.
    window_histogram: list[int]


class FreshnessOut(ApiOut):
    """Doanh nghiệp đang poll và tuổi lượt poll đủ gần nhất (giây)."""

    # Trường của `sla.Freshness` (tests/test_openapi.py kiểm khớp).
    ready: int
    by_state: dict[ScheduleState, int]
    age_p50: float | None
    age_p95: float | None
    age_max: float | None
    overdue: int
    never_polled: int


class CrawlWindowsOut(ApiOut):
    timezone: str
    build: str  # khung giờ build, vd. "00:00-06:00" (giờ CRAWL_TIMEZONE)
    poll: str
    in_build: bool
    in_poll: bool
    next_build_start: datetime
    next_poll_start: datetime


class CrawlStatusOut(ApiOut):
    target_seconds: int  # mục tiêu cửa sổ phát hiện
    poll_interval_seconds: float
    polls: PollStatsOut
    freshness: FreshnessOut
    windows: CrawlWindowsOut
