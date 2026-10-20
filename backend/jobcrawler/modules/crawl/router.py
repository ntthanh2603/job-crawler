"""API thu thập: bộ khung + lịch của doanh nghiệp, build / poll ngay, độ trễ phát hiện
(`/api/companies/{id}/crawl|build|poll`, `/api/crawl/status`)."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request

from jobcrawler.common.api import QueuedOut, actor_of, res_of
from jobcrawler.common.schemas import RunTrigger
from jobcrawler.modules.audit import audit
from jobcrawler.modules.crawl import actions, queries
from jobcrawler.modules.crawl.schemas import CompanyCrawlOut, CrawlStatusOut, PlanOut, ScheduleOut
from jobcrawler.sources.plan import load_spec

router = APIRouter(prefix="/api", tags=["api"])


def _plan(p: Any) -> PlanOut:
    spec = p.spec or {}
    loaded = load_spec(spec)
    return PlanOut(
        id=p.id,
        source=p.source,
        kind=p.kind,
        url=p.url,
        status=p.status,
        jobs=p.jobs,
        built_at=p.built_at,
        polled_at=p.polled_at,
        changed_at=p.changed_at,
        failures=p.failures,
        last_error=p.last_error,
        listings=list(spec.get("listings") or [])[:20],
        hubs=len(spec.get("hubs") or []),
        apis=[a.get("url", "") for a in spec.get("apis") or [] if not a.get("embedded")][:10],
        rendered=len(spec.get("rendered") or []),
        llm_listing=bool(spec.get("llm_listing")),
        ignored=len(spec.get("ignored") or []),
        pages=loaded.pages if loaded is not None else 0,
        pages_needed=loaded.demand() if loaded is not None else 0,
    )


@router.get("/companies/{company_id}/crawl")
async def get_company_crawl(request: Request, company_id: UUID) -> CompanyCrawlOut:
    """Bộ khung các nguồn (ghi ở lượt build) và lịch build / poll của doanh nghiệp."""
    res = res_of(request)
    async with res.sessionmaker() as session:
        schedule, plans = await queries.company_crawl(session, company_id)
    return CompanyCrawlOut(
        schedule=ScheduleOut.model_validate(schedule, from_attributes=True) if schedule else None,
        plans=[_plan(p) for p in plans],
        poll_interval_seconds=res.settings.poll_interval_seconds,
    )


@router.post("/companies/{company_id}/build")
async def post_company_build(request: Request, company_id: UUID) -> QueuedOut:
    """Build lại bộ khung ngay (lượt đủ, tìm nguồn lại)."""
    res = res_of(request)
    queued = await actions.enqueue_build(res, company_id)
    await _audit_company_run(request, audit.COMPANY_BUILD, company_id, queued)
    return QueuedOut(queued=queued)


@router.post("/companies/{company_id}/poll")
async def post_company_poll(request: Request, company_id: UUID) -> QueuedOut:
    """Poll ngay theo bộ khung."""
    res = res_of(request)
    queued = await actions.enqueue_poll(res, company_id)
    await _audit_company_run(request, audit.COMPANY_POLL, company_id, queued)
    return QueuedOut(queued=queued)


async def _audit_company_run(request: Request, action: str, company_id: UUID, queued: bool) -> None:
    await audit.record_safely(
        res_of(request).sessionmaker,
        action=action,
        actor=actor_of(request),
        resource_type="company",
        resource_id=company_id,
        metadata={"queued": queued, "trigger": RunTrigger.MANUAL.value},
    )


@router.get("/crawl/status")
async def get_crawl_status(request: Request, minutes: int = 60) -> CrawlStatusOut:
    """Độ trễ phát hiện tin mới (P50/P95 cửa sổ phát hiện của các lượt poll trong `minutes` phút
    gần nhất), độ tươi hiện tại, khung giờ build / poll."""
    status = await actions.crawl_status(res_of(request), minutes=max(1, min(minutes, 24 * 60)))
    return CrawlStatusOut.model_validate(status)
