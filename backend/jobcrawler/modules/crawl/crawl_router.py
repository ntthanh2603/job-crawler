"""API thu thập: bộ khung + lịch của doanh nghiệp, build / poll ngay, độ trễ phát hiện
(`/api/companies/{id}/crawl|build|poll`, `/api/crawl/plans/{id}`, `/api/crawl/status`)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from jobcrawler.common.api import QueuedOut, actor_of, res_of
from jobcrawler.common.config import Settings
from jobcrawler.common.schemas import RunTrigger
from jobcrawler.modules.audit import audit_service
from jobcrawler.modules.crawl import crawl_service
from jobcrawler.modules.crawl.crawl_schema import (
    CompanyCrawlOut,
    CrawlStatusOut,
    CrawlWindowsOut,
    PlanDetailOut,
    PollLimitsOut,
    ScheduleOut,
)

router = APIRouter(prefix="/api", tags=["crawl"])


def _limits(s: Settings) -> PollLimitsOut:
    return PollLimitsOut(
        max_failures=s.poll_max_failures,
        max_detail_pages=s.poll_max_detail_pages,
        max_pages=s.plan_max_pages,
    )


@router.get("/companies/{company_id}/crawl")
async def get_company_crawl(request: Request, company_id: UUID) -> CompanyCrawlOut:
    """Bộ khung các nguồn (ghi ở lượt build) và lịch build / poll của doanh nghiệp."""
    res = res_of(request)
    s = res.settings
    async with res.sessionmaker() as session:
        schedule, plans = await crawl_service.company_crawl(session, company_id)
    return CompanyCrawlOut(
        schedule=ScheduleOut.model_validate(schedule, from_attributes=True) if schedule else None,
        plans=[crawl_service.plan_view(p) for p in plans],
        poll_interval_seconds=s.poll_interval_seconds,
        limits=_limits(s),
        windows=CrawlWindowsOut.model_validate(crawl_service.windows(s, datetime.now(UTC))),
    )


@router.get("/crawl/plans/{plan_id}")
async def get_crawl_plan(request: Request, plan_id: UUID) -> PlanDetailOut:
    """Toàn bộ một bộ khung: trang / API lượt poll đọc lại, mã tin (kèm tin đã lưu), link bỏ qua,
    danh mục mã → tên, JSON gốc."""
    res = res_of(request)
    async with res.sessionmaker() as session:
        found = await crawl_service.plan_detail(session, plan_id)
    if found is None:
        raise HTTPException(404, "Không có bộ khung này")
    plan, company, postings = found
    return crawl_service.plan_detail_view(plan, company, postings, _limits(res.settings))


@router.post("/companies/{company_id}/build")
async def post_company_build(request: Request, company_id: UUID) -> QueuedOut:
    """Build lại bộ khung ngay (lượt đủ, tìm nguồn lại)."""
    res = res_of(request)
    queued = await crawl_service.enqueue_build(res, company_id)
    await _audit_company_run(request, audit_service.COMPANY_BUILD, company_id, queued)
    return QueuedOut(queued=queued)


@router.post("/companies/{company_id}/poll")
async def post_company_poll(request: Request, company_id: UUID) -> QueuedOut:
    """Poll ngay theo bộ khung."""
    res = res_of(request)
    queued = await crawl_service.enqueue_poll(res, company_id)
    await _audit_company_run(request, audit_service.COMPANY_POLL, company_id, queued)
    return QueuedOut(queued=queued)


async def _audit_company_run(request: Request, action: str, company_id: UUID, queued: bool) -> None:
    await audit_service.record_safely(
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
    status = await crawl_service.crawl_status(
        res_of(request), minutes=max(1, min(minutes, 24 * 60))
    )
    return CrawlStatusOut.model_validate(status)
