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
    request_id: str | None
    # {key: {"before": ..., "after": ...}}; bí mật đã che.
    changes: dict[str, Any] | None
    metadata: dict[str, Any] | None


class AuditPageOut(ApiOut):
    items: list[AuditEventOut]
    # Id dòng cuối trang: truyền vào `before` để lấy trang sau; null khi đã hết.
    next: UUID | None
