"""Mô hình API cho hệ thống ngoài (`/api/integration/v1`, docs/INTEGRATION.md): việc làm kèm mã
doanh nghiệp bên ngoài, sự kiện feed, trang snapshot."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field

from jobcrawler.common.api import ApiOut
from jobcrawler.common.schemas import JobEventType, JobOut


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
