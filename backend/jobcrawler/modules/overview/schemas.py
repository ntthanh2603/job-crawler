"""Mô hình trả về của trang Tổng quan và `/api/stats`."""

from __future__ import annotations

from datetime import date
from typing import Literal
from uuid import UUID

from jobcrawler.common.api import ApiOut, Timestamp
from jobcrawler.modules.runs.schemas import RunOut


class QueueStatsOut(ApiOut):
    # Trường của `stats.QueueStats` (tests/test_openapi.py kiểm khớp).
    name: str
    queued: int
    active: int
    scheduled: int
    workers: int
    capacity: int
    complete: int
    failed: int
    retried: int
    aborted: int
    error: str | None


class RunStatsOut(ApiOut):
    # Trường của `stats.RunStats` (tests/test_openapi.py kiểm khớp).
    since: Timestamp | None
    runs: int
    companies: int
    by_status: dict[str, int]
    latest_by_status: dict[str, int]
    failed_rate: float | None
    p50_seconds: float | None
    p95_seconds: float | None
    max_seconds: float | None
    over_timeout: int
    wait_p50_seconds: float | None
    wait_p95_seconds: float | None
    jobs_found: int
    jobs_accepted: int
    companies_with_jobs: int
    first_started: Timestamp | None
    last_finished: Timestamp | None
    per_hour: float | None
    incomplete_sources: list[tuple[str, int]]
    errors: list[tuple[str, int]]


class SourceStatsOut(ApiOut):
    # Trường của `stats.SourceStats` (tests/test_openapi.py kiểm khớp).
    kind: str
    fetches: int
    outcomes: dict[str, int]
    avg_ms: int | None
    cached: int


class BoardStatsOut(ApiOut):
    # Trường của `stats.BoardStats` (tests/test_openapi.py kiểm khớp).
    source: str
    active_postings: int
    postings: int
    profiles: int
    profiles_mapped: int
    last_status: str | None
    last_started: Timestamp | None
    last_finished: Timestamp | None
    last_complete: bool | None
    last_urls_seen: int | None
    last_fetched: int | None
    last_new: int | None
    last_removed: int | None


class StorageStatsOut(ApiOut):
    # Trường của `compress.StorageStats` (tests/test_openapi.py kiểm khớp).
    kind: str
    table: str
    values: int
    stored_bytes: int
    original_bytes: int
    by_dict: dict[int, int] | None  # id từ điển zstd → số giá trị


class TargetOut(ApiOut):
    # Trường của `stats.Target` (tests/test_openapi.py kiểm khớp).
    name: str
    value: float | None
    target: str
    ok: bool | None
    kind: Literal["seconds", "rate"]


class StatsOut(ApiOut):
    # Trường của `stats.Stats` (tests/test_openapi.py kiểm khớp).
    generated_at: Timestamp
    queues: list[QueueStatsOut]
    companies: int
    kyc_status: dict[str, int]
    jobs_by_status: dict[str, int]
    jobs_by_source: list[tuple[str, int]]
    runs: RunStatsOut
    sources: list[SourceStatsOut]
    boards: list[BoardStatsOut]
    storage: list[StorageStatsOut]
    tables: list[tuple[str, int]]  # (bảng, byte trên đĩa gồm index và TOAST)
    targets: list[TargetOut]


class RunsHourOut(ApiOut):
    hour: Timestamp
    counts: dict[str, int]  # trạng thái → số lượt
    total: int


class FetchesHourOut(ApiOut):
    hour: Timestamp
    ok: int
    blocked: int
    error: int


class ChartTotalsOut(ApiOut):
    active_jobs: int
    companies: int
    companies_with_jobs: int
    new_jobs_7d: int
    with_salary: int


class JobsDayOut(ApiOut):
    day: date  # ngày giờ Việt Nam
    careers: int
    boards: int


class TopCompanyOut(ApiOut):
    id: UUID
    name: str
    active_jobs: int


class ChartsOut(ApiOut):
    """Phân bố trên job đang mở / doanh nghiệp (tính sẵn bằng cron); các cặp là (tên, số)."""

    generated_at: Timestamp
    totals: ChartTotalsOut
    jobs_by_day: list[JobsDayOut]
    jobs_by_source: list[tuple[str, int]]
    jobs_by_province: list[tuple[str, int]]
    jobs_by_level: list[tuple[str, int]]
    jobs_by_employment: list[tuple[str, int]]
    jobs_by_work_mode: list[tuple[str, int]]
    salary_bands: list[tuple[str, int]]
    kyc: list[tuple[str, int]]
    top_companies: list[TopCompanyOut]


class RecentRunOut(RunOut):
    company_name: str


class OverviewOut(ApiOut):
    stats: StatsOut
    chart: list[RunsHourOut]  # lượt chạy theo giờ, 24 giờ gần nhất
    fetch_chart: list[FetchesHourOut]  # request theo giờ, 24 giờ gần nhất
    charts: ChartsOut
    recent_runs: list[RecentRunOut]
