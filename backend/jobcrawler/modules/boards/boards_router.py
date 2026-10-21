"""API trang tuyển dụng: `/api/boards…`."""

from __future__ import annotations

import dataclasses
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request

from jobcrawler.common.api import PageOut, actor_of, page_out, res_of
from jobcrawler.modules.audit import audit_service
from jobcrawler.modules.boards import boards_service
from jobcrawler.modules.boards.boards_schema import BoardIndexOut, BoardRunOut, BoardsOut
from jobcrawler.modules.overview import stats_service

router = APIRouter(prefix="/api", tags=["boards"])


def _board_run(r: Any) -> BoardRunOut:
    return BoardRunOut(
        id=r.id,
        source=r.source,
        started_at=r.started_at,
        finished_at=r.finished_at,
        status=r.status,
        complete=r.complete,
        urls_seen=r.urls_seen,
        fetched=r.fetched,
        new=r.new,
        updated=r.updated,
        unchanged=r.unchanged,
        failed=r.failed,
        removed=r.removed,
        profiles_new=r.profiles_new,
        profiles_matched=r.profiles_matched,
        error=r.error,
        mode=(r.stats or {}).get("mode", "full"),
    )


@router.get("/boards")
async def get_boards(request: Request) -> BoardsOut:
    async with res_of(request).sessionmaker() as session:
        board_rows = await stats_service.board_stats(session)
    known = {b.source for b in board_rows}
    board_rows += [
        stats_service.BoardStats(source=n) for n in boards_service.board_names() if n not in known
    ]
    queue = await stats_service.queue_stats(res_of(request).queues.boards)
    return BoardsOut.model_validate(
        {
            "boards": [dataclasses.asdict(b) for b in board_rows],
            "queue": dataclasses.asdict(queue),
        }
    )


@router.get("/boards/runs")
async def get_board_runs(
    request: Request, query: Annotated[boards_service.BoardRunsQuery, Query()]
) -> PageOut[BoardRunOut]:
    async with res_of(request).sessionmaker() as session:
        result = await boards_service.board_runs(session, query)
    return page_out(result, [_board_run(r) for r in result.items])


@router.post("/boards/{name}/index")
async def post_board_index(request: Request, name: str) -> BoardIndexOut:
    res = res_of(request)
    try:
        queued = await boards_service.enqueue_board(res, name)
    except KeyError:
        raise HTTPException(404, "Không có trang này") from None
    await audit_service.record_safely(
        res.sessionmaker,
        action=audit_service.BOARD_INDEX,
        actor=actor_of(request),
        resource_type="board",
        resource_id=name,
        metadata={"queued": queued},
    )
    queue = await stats_service.queue_stats(res.queues.boards)
    return BoardIndexOut(queued=queued, workers=queue.workers)
