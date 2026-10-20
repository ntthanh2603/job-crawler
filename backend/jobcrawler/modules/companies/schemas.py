"""Mô hình vào / ra của API doanh nghiệp."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from jobcrawler.common.api import ApiOut
from jobcrawler.common.schemas import CompanyEntity
from jobcrawler.modules.runs.schemas import FailedPageItem


class CompanyItem(ApiOut):
    company: CompanyEntity
    input_website: str | None
    last_status: str | None
    last_started: datetime | None
    last_duration_ms: int | None
    last_incomplete: list[str]
    active_jobs: int


class AliasOut(ApiOut):
    alias: str
    kind: str | None


class ProfileOut(ApiOut):
    source: str
    url: str
    name_on_source: str
    website: str | None
    tax_code: str | None
    match_confidence: float | None
    match_signal: str | None
    indexed_at: datetime | None


class CompanyDetailOut(ApiOut):
    company: CompanyEntity
    input_website: str | None
    note: str | None
    created_at: datetime
    kyc_evidence: Any
    aliases: list[AliasOut]
    profiles: list[ProfileOut]
    # Số đếm cho các tab; danh sách lấy theo trang ở /companies/{id}/jobs|runs|rejected.
    active_jobs: int
    inactive_jobs: int
    runs_total: int
    rejected_total: int
    failed_pages: list[FailedPageItem]


class EnqueueIn(BaseModel):
    company_ids: list[UUID] | None = Field(default=None, description="Các doanh nghiệp cần chạy.")
    stale_hours: float | None = Field(
        default=None, description="Chỉ doanh nghiệp chưa có lượt chạy xong trong N giờ."
    )
    all: bool = Field(default=False, description="Mọi doanh nghiệp (kết hợp stale_hours).")


class CompanyCreateIn(BaseModel):
    name: str = Field(min_length=1, description="Tên doanh nghiệp (như cột `name` của CSV).")
    tax_code: str | None = None
    website: str | None = None
    note: str | None = None
    enqueue: bool = Field(default=False, description="Đưa vào hàng đợi build ngay.")


class ImportOutcomeOut(ApiOut):
    # Trường của `actions.ImportOutcome` (tests/test_openapi.py kiểm khớp).
    rows: int
    created: int
    updated: int
    unchanged: int
    duplicates: int
    errors: list[str]
    warnings: list[str]
    enqueued: int | None
    already_queued: int


class CompanyCreatedOut(ApiOut):
    # Trường của `actions.CreateOutcome` (tests/test_openapi.py kiểm khớp).
    id: UUID
    created: bool  # False: đã có doanh nghiệp này, chỉ bổ sung trường còn trống
    warnings: list[str]
    queued: bool | None  # None: không yêu cầu đưa vào hàng đợi


class EnqueueResultOut(ApiOut):
    # Trường của `batch.EnqueueResult` (tests/test_openapi.py kiểm khớp).
    selected: int
    enqueued: int
    already_queued: int
