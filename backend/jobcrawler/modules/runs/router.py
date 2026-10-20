"""API lượt chạy: `/api/runs/…` (chi tiết, request, log) và `/api/failed-pages/…`."""

from __future__ import annotations

import uuid
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from jobcrawler.common.api import PageOut, page_out, res_of
from jobcrawler.modules.runs import queries
from jobcrawler.modules.runs.schemas import (
    FailedPageOut,
    FetchOut,
    LogOut,
    RunDetailOut,
    SourceCount,
    failed_page_item,
    run_out,
)
from jobcrawler.services.resources import Resources

router = APIRouter(prefix="/api", tags=["api"])


async def load_run_detail(res: Resources, run_id: uuid.UUID) -> RunDetailOut | None:
    """Tóm tắt lượt chạy như `GET /api/runs/{run_id}` (cũng là chủ đề `run` của
    `modules/live/live.py`)."""
    async with res.sessionmaker() as session:
        d = await queries.run_detail(session, run_id)
    if d is None:
        return None
    company = d.company
    return RunDetailOut(
        run=run_out(d.run),
        company_name=(company.brand_name or company.legal_name or company.input_name)
        if company
        else None,
        by_source=[SourceCount(source=s, total=n, outcomes=o) for s, n, o in d.by_source],
        fetches_total=d.fetches,
        logs_total=d.logs,
        failed_pages=[failed_page_item(f) for f in d.failed_pages],
    )


@router.get("/runs/{run_id}")
async def get_run(request: Request, run_id: uuid.UUID) -> RunDetailOut:
    detail = await load_run_detail(res_of(request), run_id)
    if detail is None:
        raise HTTPException(404, "Không có lượt chạy này")
    return detail


@router.get("/runs/{run_id}/fetches")
async def get_run_fetches(request: Request, run_id: uuid.UUID, page: int = 1) -> PageOut[FetchOut]:
    async with res_of(request).sessionmaker() as session:
        result = await queries.run_fetches(session, run_id, page)
    return page_out(
        result,
        [
            FetchOut(
                source=f.source,
                url=f.url,
                http_status=f.http_status,
                outcome=f.outcome,
                duration_ms=f.duration_ms,
                items=f.items,
                error=f.error,
            )
            for f in result.items
        ],
    )


@router.get("/runs/{run_id}/logs")
async def get_run_logs(request: Request, run_id: uuid.UUID, page: int = 1) -> PageOut[LogOut]:
    async with res_of(request).sessionmaker() as session:
        result = await queries.run_logs(session, run_id, page)
    return page_out(
        result,
        [LogOut(ts=x.ts, level=x.level, event=x.event, context=x.context) for x in result.items],
    )


@router.get("/failed-pages/{page_id}")
async def get_failed_page(request: Request, page_id: UUID) -> FailedPageOut:
    async with res_of(request).sessionmaker() as session:
        page = await queries.failed_page(session, page_id)
    if page is None:
        raise HTTPException(404, "Không có trang này (đã quá hạn lưu?)")
    return FailedPageOut(
        id=page.id,
        url=page.url,
        source=page.source,
        reason=page.reason,
        created_at=page.created_at,
        http_status=page.http_status,
        run_id=str(page.run_id) if page.run_id else None,
        html=page.html or "",
    )


@router.get("/failed-pages/{page_id}/raw")
async def get_failed_page_raw(request: Request, page_id: UUID) -> PlainTextResponse:
    async with res_of(request).sessionmaker() as session:
        page = await queries.failed_page(session, page_id)
    if page is None:
        raise HTTPException(404)
    # text/plain: không để trình duyệt chạy HTML lấy từ site khác.
    return PlainTextResponse(
        page.html or "",
        headers={"Content-Disposition": f'attachment; filename="failed-page-{page_id}.html"'},
    )
