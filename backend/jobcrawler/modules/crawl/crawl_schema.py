"""Mô hình trả về của API thu thập (bộ khung, lịch, độ trễ phát hiện)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from jobcrawler.common.api import ApiOut
from jobcrawler.common.schemas import JobStatus, PlanStatus, ScheduleState


class PlanPollResult(StrEnum):
    """Lượt poll gần nhất của một nguồn đi đường nào (suy từ cột của `crawl_plans`: lượt poll
    không đổi gì không ghi `company_runs`)."""

    NEVER = "never"  # chưa poll kể từ lượt build ghi bộ khung
    UNCHANGED = "unchanged"  # tập mã tin như lượt trước: chỉ cập nhật lịch
    CHANGED = "changed"  # có tin mới / tin gỡ: xử lý tin mới, ghi lượt chạy
    RETRYING = "retrying"  # lỗi tạm, chưa tới POLL_MAX_FAILURES lượt liên tiếp
    BROKEN = "broken"  # bộ khung không còn đúng: đã xếp build lại
    RETIRED = "retired"  # lượt build gần nhất không còn thấy nguồn: không poll


class PlanPageOut(ApiOut):
    """Một trang lượt poll đọc lại (`PlanSpec.listings` + `hubs`)."""

    url: str
    hub: bool  # trang danh mục / theo tỉnh dẫn tới tin (False: trang danh sách)
    rendered: bool  # lượt build phải mở bằng trình duyệt: poll đọc HTTP trước, trống mới render
    api_only: bool  # mọi tin có trong API bắt được: API trả tin thì poll không mở trang này


class PlanApiOut(ApiOut):
    """Một API danh sách tin (`PlanSpec.apis`): poll gọi thẳng bằng HTTP."""

    url: str
    method: str
    embedded: bool  # JSON nhúng trong HTML (`__NEXT_DATA__`...): đọc cùng trang, không gọi riêng
    paged: bool
    page_param: str  # tham số trang / offset trong request
    by_offset: bool  # phân trang theo offset (False: theo số trang)
    size: int  # số tin mỗi trang lúc build (0: không rõ)
    total: int | None  # tổng số tin API báo lúc build
    pages: int  # số trang để đọc trọn (API nhúng: trang đầu nằm sẵn trong HTML)
    listing: str | None  # trang danh sách đã gọi API này


class PlanOut(ApiOut):
    id: UUID
    source: str
    kind: str
    url: str
    status: PlanStatus
    jobs: int  # số mã tin lượt đủ gần nhất (`job_keys`): lượt poll so với tập này
    built_at: datetime
    polled_at: datetime | None
    changed_at: datetime | None
    failures: int
    last_error: str | None
    last_poll: PlanPollResult
    # False: bộ khung ghi theo phiên bản cũ / không đọc được — lượt poll đánh hỏng và build lại.
    readable: bool
    ats: str | None  # loại ATS của bảng tin (greenhouse, workday...); None: trang careers
    # Trang danh sách + danh mục (tối đa PLAN_LIST_LIMIT) và tổng số trang trong bộ khung.
    listings: list[PlanPageOut]
    listings_total: int
    apis: list[PlanApiOut]
    apis_total: int
    llm_listing: bool
    ignored: int  # link đã mở ở lượt build mà không phải tin: poll không mở lại
    indirect: int  # mã tin chỉ tới được qua trang tin khác: poll không thấy lại vẫn giữ
    # Số trang (danh sách, danh mục, API) một lượt poll được đọc ở nguồn này (0 = bộ khung cũ,
    # dùng PLAN_MAX_PAGES) và số trang cần để đọc trọn nguồn.
    pages: int
    pages_needed: int


class PlanJobOut(ApiOut):
    """Một mã tin trong bộ khung (`crawl_plans.job_keys`) và tin đã lưu của nó (nếu có)."""

    key: str  # mã tin ở nguồn (`job_postings.external_id`)
    indirect: bool  # chỉ tới được qua trang tin khác: poll không thấy lại vẫn giữ
    url: str | None
    title: str | None  # tiêu đề việc đã gộp (None: tin chưa lưu hoặc bị loại)
    status: JobStatus | None  # trạng thái tin trên nguồn
    accepted: bool | None  # kết quả xác minh (False: tin bị loại)
    job_id: UUID | None


class PlanNameOut(ApiOut):
    """Một mục danh mục mã → tên tải kèm trang (`PlanSpec.names`), vd. tỉnh `33` → "Hà Nội"."""

    kind: str
    id: str
    name: str


class PollLimitsOut(ApiOut):
    """Giới hạn của lượt poll (cài đặt `POLL_MAX_FAILURES`, `POLL_MAX_DETAIL_PAGES`,
    `PLAN_MAX_PAGES`)."""

    max_failures: int  # lỗi tạm liên tiếp chừng này lượt thì bộ khung hỏng, build lại
    max_detail_pages: int  # trang chi tiết (tin mới) tối đa mỗi lượt poll
    max_pages: int  # trang danh sách / API tối đa mỗi lượt poll của doanh nghiệp


class PlanDetailOut(ApiOut):
    """Toàn bộ một bộ khung: trang / API (như `PlanOut`), các mã tin, link bỏ qua, danh mục
    mã → tên và JSON gốc."""

    company_id: UUID
    company_name: str
    plan: PlanOut
    limits: PollLimitsOut
    # Mã tin theo thứ tự lượt đọc trọn gần nhất (tối đa PLAN_JOB_LIMIT) và tổng số.
    jobs: list[PlanJobOut]
    jobs_total: int
    ignored_links: list[str]
    names: list[PlanNameOut]
    names_total: int
    spec: dict[str, Any]  # `crawl_plans.spec` nguyên văn


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


class CompanyCrawlOut(ApiOut):
    schedule: ScheduleOut | None
    plans: list[PlanOut]
    poll_interval_seconds: float
    limits: PollLimitsOut
    windows: CrawlWindowsOut
