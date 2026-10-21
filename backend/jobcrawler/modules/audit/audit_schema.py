"""Mô hình trả về của API nhật ký thao tác."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from jobcrawler.common.api import ApiOut


class AuditEventOut(ApiOut):
    id: UUID
    occurred_at: datetime
    actor: str
    action: str  # tên có chấm: `settings.update`, `company.build`... (GET /api/audit/actions)
    resource_type: str | None
    resource_id: str | None
    # Tên hiện của đối tượng (tên doanh nghiệp, tiêu đề việc làm), tra lúc đọc; null khi không có.
    resource_label: str | None
    request_id: str | None
    ip: str | None  # IP người dùng của request (sau reverse proxy tin cậy là IP thật)
    user_agent: str | None
    # {key: {"before": ..., "after": ...}}; bí mật đã che.
    changes: dict[str, Any] | None
    metadata: dict[str, Any] | None


class AuditPageOut(ApiOut):
    items: list[AuditEventOut]
    # Id dòng cuối trang: truyền vào `before` để lấy trang sau; null khi đã hết.
    next: UUID | None
