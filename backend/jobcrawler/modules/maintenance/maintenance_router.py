"""API `/api/maintenance`: chạy bảo trì ngay."""

from __future__ import annotations

from fastapi import APIRouter, Request

from jobcrawler.common.api import QueuedOut, actor_of, res_of
from jobcrawler.modules.audit import audit_service
from jobcrawler.modules.maintenance import maintenance_service

router = APIRouter(prefix="/api", tags=["maintenance"])


@router.post("/maintenance")
async def post_maintenance(request: Request) -> QueuedOut:
    res = res_of(request)
    queued = await maintenance_service.enqueue_maintenance(res)
    await audit_service.record_safely(
        res.sessionmaker,
        action=audit_service.MAINTENANCE_RUN,
        actor=actor_of(request),
        metadata={"queued": queued},
    )
    return QueuedOut(queued=queued)
