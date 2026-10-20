"""API trang Hàng đợi: `/api/queue`."""

from __future__ import annotations

import dataclasses
import math
from typing import Any

from fastapi import APIRouter, Request

from jobcrawler.common.api import res_of
from jobcrawler.modules.companies import queries as company_queries
from jobcrawler.modules.overview import stats
from jobcrawler.modules.queue.schemas import QueueJobOut, QueueOut, QueuePageOut
from jobcrawler.services.resources import Resources

router = APIRouter(prefix="/api", tags=["api"])


def _queue_job(job: dict[str, Any]) -> QueueJobOut:
    kwargs = job.get("kwargs") or {}
    return QueueJobOut(
        key=job["key"],
        function=job["function"],
        status=job["status"],
        attempts=job["attempts"],
        retries=job["retries"],
        timeout=job["timeout"],
        queued=job["queued"],
        started=job["started"],
        scheduled=job.get("scheduled") or 0,
        error=job.get("error"),
        label=kwargs.get("name") or kwargs.get("source") or "",
        company_id=kwargs.get("company_id"),
        trigger=kwargs.get("trigger"),
        continuation=kwargs.get("continuation") or 0,
    )


QUEUE_PAGE = 25


async def load_queue_page(
    res: Resources, *, page: int = 1, poll_page: int = 1, board_page: int = 1
) -> QueuePageOut:
    """Ba queue như `GET /api/queue` (cũng là chủ đề `queue` của `modules/live/live.py`)."""
    out: list[QueueOut] = []
    queues = res.queues
    wanted_pages = ((queues.build, page), (queues.poll, poll_page), (queues.boards, board_page))
    for queue, wanted in wanted_pages:
        st = await stats.queue_stats(queue)
        listed = st.queued + st.active + st.scheduled
        pages = max(1, math.ceil(listed / QUEUE_PAGE))
        current = min(max(1, wanted), pages)
        try:
            info = await queue.info(jobs=True, offset=(current - 1) * QUEUE_PAGE, limit=QUEUE_PAGE)
            jobs = [_queue_job(j) for j in info.get("jobs") or []]
        except Exception:  # Redis lỗi: vẫn trả số đếm
            jobs = []
        out.append(
            QueueOut(**dataclasses.asdict(st), jobs=jobs, page=current, pages=pages, total=listed)
        )
    # Job build / poll chỉ mang id doanh nghiệp: điền tên để hiện.
    ids = {j.company_id for q in out for j in q.jobs if j.company_id and not j.label}
    if ids:
        async with res.sessionmaker() as session:
            names = await company_queries.company_names(session, ids)
        for q in out:
            for j in q.jobs:
                if not j.label and j.company_id:
                    j.label = names.get(j.company_id, "")
    return QueuePageOut(queues=out)


@router.get("/queue")
async def get_queue(
    request: Request, page: int = 1, poll_page: int = 1, board_page: int = 1
) -> QueuePageOut:
    """Ba queue (build, poll, trang tuyển dụng): số job, runner, và một trang các job đang
    chạy / đang chờ / hẹn giờ của mỗi queue (`page`, `poll_page`, `board_page`; mỗi trang
    QUEUE_PAGE job)."""
    return await load_queue_page(
        res_of(request), page=page, poll_page=poll_page, board_page=board_page
    )
