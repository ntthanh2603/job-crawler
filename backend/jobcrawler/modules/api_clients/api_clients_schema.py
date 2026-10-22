"""Mô hình vào / ra của API quản lý hệ thống ngoài (trang Tích hợp)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from jobcrawler.common.api import ApiOut

Scope = Literal["companies:write", "jobs:read"]


class ApiClientIn(BaseModel):
    name: str = Field(min_length=1, max_length=100, description="Tên hệ thống ngoài")
    scopes: list[Scope] = Field(
        default_factory=lambda: ["companies:write", "jobs:read"],
        description="companies:write: đẩy doanh nghiệp; jobs:read: đọc việc làm / feed",
    )
    all_companies: bool = Field(
        default=False, description="Đọc mọi việc làm (không chỉ doanh nghiệp đã đẩy)"
    )
    include_description: bool = Field(
        default=True, description="Gửi kèm mô tả / yêu cầu / quyền lợi"
    )


class ApiClientUpdateIn(BaseModel):
    """Chỉ trường có mặt được đổi."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    scopes: list[Scope] | None = None
    all_companies: bool | None = None
    include_description: bool | None = None


class ApiKeyOut(ApiOut):
    id: UUID
    prefix: str  # vài ký tự đầu sau `jca_`
    created_by: str | None
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class ApiClientOut(ApiOut):
    id: UUID
    name: str
    scopes: list[str]
    all_companies: bool
    include_description: bool
    companies: int  # số mã doanh nghiệp đã đẩy
    active_keys: int
    last_used_at: datetime | None
    created_by: str | None
    created_at: datetime


class ApiClientDetailOut(ApiClientOut):
    keys: list[ApiKeyOut]


class ApiKeyCreatedOut(ApiOut):
    """Khoá chỉ hiện một lần: hệ thống ngoài gửi `Authorization: Bearer <key>`."""

    id: UUID
    prefix: str
    key: str
    created_at: datetime
