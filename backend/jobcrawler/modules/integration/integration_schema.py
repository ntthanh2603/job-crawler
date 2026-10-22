"""Mô hình API cho hệ thống ngoài (`/api/integration/v1`, docs/INTEGRATION.md): việc làm kèm mã
doanh nghiệp bên ngoài, sự kiện feed, trang snapshot."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, Field

from jobcrawler.common.api import ApiOut, known_values
from jobcrawler.common.schemas import JobEventType, JobOut, KycStatus, ScheduleState


class IntegrationJobOut(JobOut):
    """Việc làm như `JobOut`, kèm id dòng `jobs` (khoá để bên nhận upsert) và mã doanh nghiệp bên
    ngoài của client. `status` đã tính hết hạn theo `expires_at`. Client tắt mô tả thì
    `description_md` rỗng, `requirements_md` / `benefits_md` null."""

    id: UUID
    company_external_ids: list[str] = Field(default_factory=list)
    updated_at: datetime


class JobEventOut(ApiOut):
    """Một sự kiện: việc `job_id` vừa được tạo / đổi / đóng / mở lại / xoá. `job` là trạng thái
    hiện tại của việc lúc đọc (không phải lúc xảy ra) — bên nhận upsert theo `job.id`; null khi
    việc đã bị xoá."""

    seq: int
    type: JobEventType
    occurred_at: datetime
    job_id: UUID
    company_id: UUID
    job: IntegrationJobOut | None


class JobChangesOut(ApiOut):
    """Một trang feed thay đổi. Gọi tiếp với `cursor=next_cursor`; `has_more=false` thì đã tới
    `head` (seq mới nhất), lượt sau có thể chờ (`wait`)."""

    events: list[JobEventOut]
    next_cursor: str
    has_more: bool
    head: str


class JobSnapshotOut(ApiOut):
    """Một trang snapshot việc làm (theo id tăng dần). `next_cursor` null: hết. `feed_cursor` chỉ
    có ở trang đầu: đọc feed thay đổi từ mốc này sau khi lấy hết snapshot."""

    data: list[IntegrationJobOut]
    next_cursor: str | None
    feed_cursor: str | None


# --- doanh nghiệp --------------------------------------------------------------------------------

EXTERNAL_ID_DESCRIPTION = "Mã doanh nghiệp bên hệ thống ngoài: chữ, số, `. _ : @ -`, ≤ 128 ký tự."


class CompanyUpsertIn(BaseModel):
    """Một doanh nghiệp của hệ thống ngoài (như một dòng CSV). MST / website / trang tuyển dụng sai
    định dạng thì bỏ trường đó (cảnh báo trong kết quả), không từ chối cả doanh nghiệp."""

    name: str = Field(min_length=1, max_length=300, description="Tên doanh nghiệp")
    tax_code: str | None = Field(
        default=None, max_length=32, description="Mã số thuế (10 số, chi nhánh thêm -3 số)"
    )
    website: str | None = Field(default=None, max_length=500, description="Website / domain")
    careers_url: str | None = Field(
        default=None,
        max_length=1000,
        description="Trang tuyển dụng hoặc bảng ATS (nếu biết): tìm nguồn thử trước",
    )
    note: str | None = Field(default=None, max_length=1000)
    active: bool = Field(
        default=True, description="false: ngừng thu thập (khi mọi mã của doanh nghiệp đều tắt)"
    )


class CompanyBatchItemIn(CompanyUpsertIn):
    external_id: str = Field(min_length=1, max_length=128, description=EXTERNAL_ID_DESCRIPTION)


class CompanyBatchIn(BaseModel):
    items: list[CompanyBatchItemIn] = Field(min_length=1, max_length=1000)


class CompanyUpsertOut(ApiOut):
    """Kết quả một doanh nghiệp: `created` (doanh nghiệp mới), `linked` (gắn mã vào doanh nghiệp
    đã có sẵn), `updated` / `unchanged` (mã đã có), `error` (không ghi, xem `error`)."""

    external_id: str
    result: Annotated[str, known_values(("created", "linked", "updated", "unchanged", "error"))]
    company_id: UUID | None
    active: bool
    warnings: list[str]
    error: str | None


class CompanyBatchOut(ApiOut):
    results: list[CompanyUpsertOut]
    created: int
    linked: int
    updated: int
    unchanged: int
    errors: int


class CompanyImportOut(CompanyBatchOut):
    """Nạp CSV: kết quả từng dòng hợp lệ, cộng lỗi / cảnh báo theo dòng của file."""

    rows: int
    duplicates: int
    csv_errors: list[str]
    csv_warnings: list[str]


class IntegrationCompanyOut(ApiOut):
    """Trạng thái thu thập của một doanh nghiệp theo mã bên ngoài."""

    external_id: str
    active: bool
    company_id: UUID
    name: str
    legal_name: str | None
    tax_code: str | None
    website: str | None
    careers_url: str | None
    primary_domain: str | None
    kyc_status: Annotated[str | None, known_values(KycStatus, nullable=True)]
    # Lịch thu thập: new (chờ build lần đầu), ready (đang poll), no_sources (không có nguồn để
    # poll), broken (bộ khung hỏng, chờ build lại), paused (đã tắt); null: chưa xếp lịch.
    crawl_state: Annotated[str | None, known_values(ScheduleState, nullable=True)]
    sources: int
    active_jobs: int
    last_build_at: datetime | None
    last_poll_ok_at: datetime | None
    next_poll_at: datetime | None
    last_error: str | None
    linked_at: datetime


class RefreshOut(ApiOut):
    """`poll` (đọc lại bộ khung) hoặc `build` (chưa có bộ khung dùng được); `queued=false`: đã có
    lượt đang chờ / chạy, hoặc vừa gọi trong `REFRESH_SECONDS` giây."""

    kind: Annotated[str, known_values(("poll", "build"))]
    queued: bool
