"""API `/api/maintenance`: chạy bảo trì ngay."""

from __future__ import annotations

from fastapi import APIRouter, Request

from jobcrawler.common.api import QueuedOut, actor_of, res_of
from jobcrawler.modules.audit import audit
from jobcrawler.modules.maintenance import actions

router = APIRouter(prefix="/api", tags=["api"])


@router.post("/maintenance")
async def post_maintenance(request: Request) -> QueuedOut:
    res = res_of(request)
    queued = await actions.enqueue_maintenance(res)
    await audit.record_safely(
        res.sessionmaker,
        action=audit.MAINTENANCE_RUN,
        actor=actor_of(request),
        metadata={"queued": queued},
    )
    return QueuedOut(queued=queued)
