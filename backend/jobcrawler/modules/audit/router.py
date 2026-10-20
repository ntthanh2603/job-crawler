"""API nhật ký thao tác: `/api/audit…`."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, Request

from jobcrawler.common.api import res_of
from jobcrawler.modules.audit import audit
from jobcrawler.modules.audit.schemas import AuditEventOut, AuditPageOut

router = APIRouter(prefix="/api", tags=["api"])


@router.get("/audit")
async def get_audit(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=audit.MAX_LIMIT)] = 50,
    before: Annotated[
        UUID | None, Query(description="Id dòng cuối trang trước (`next`): lấy các dòng cũ hơn.")
    ] = None,
    action: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    actor: str | None = None,
) -> AuditPageOut:
    """Nhật ký thao tác của người vận hành, mới nhất trước. `next` = id dòng cuối (truyền vào
    `before` để lấy trang sau), `null` khi đã hết."""
    async with res_of(request).sessionmaker() as session:
        events, next_id = await audit.list_events(
            session,
            limit=limit,
            before=before,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            actor=actor,
        )
    return AuditPageOut(
        items=[
            AuditEventOut(
                id=e.id,
                occurred_at=e.occurred_at,
                actor=e.actor,
                action=e.action,
                resource_type=e.resource_type,
                resource_id=e.resource_id,
                request_id=e.request_id,
                changes=e.changes,
                metadata=e.meta,
            )
            for e in events
        ],
        next=next_id,
    )


@router.get("/audit/actions")
async def get_audit_actions() -> list[str]:
    """Mọi tên thao tác được ghi nhật ký (danh sách lọc)."""
    return sorted(audit.ACTIONS)
