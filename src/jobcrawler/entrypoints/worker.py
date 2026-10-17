"""Cấu hình worker SAQ: functions, cron jobs, startup/shutdown dùng `open_resources`."""

from __future__ import annotations

import dataclasses
from contextlib import AsyncExitStack
from typing import Any, Required

from saq import CronJob, Queue, Worker
from saq.types import Context

from jobcrawler.config import Settings
from jobcrawler.logging import get_logger
from jobcrawler.resources import Resources, open_resources
from jobcrawler.schemas import CompanyIn
from jobcrawler.services import boards, companies

log = get_logger(__name__)


class WorkerContext(Context, total=False):
    # `settings` gắn trong build_worker, `resources` gắn trong startup, trước mọi job.
    settings: Required[Settings]
    resources: Required[Resources]
    exit_stack: AsyncExitStack


async def startup(ctx: WorkerContext) -> None:
    stack = AsyncExitStack()
    ctx["exit_stack"] = stack
    ctx["resources"] = await stack.enter_async_context(
        open_resources(ctx["settings"], queue=ctx["worker"].queue)
    )
    log.info("worker.startup", concurrency=ctx["worker"].concurrency)


async def shutdown(ctx: WorkerContext) -> None:
    log.info("worker.shutdown")
    stack = ctx.get("exit_stack")
    if stack is not None:
        await stack.aclose()


async def process_company(
    ctx: WorkerContext,
    *,
    name: str,
    tax_code: str | None = None,
    website: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    company_in = CompanyIn(name=name, tax_code=tax_code, website=website, note=note)
    result = await companies.process_company(ctx["resources"], company_in)
    return result.model_dump(mode="json", exclude={"jobs"})


def board_job_timeout(settings: Settings) -> int:
    # Ngân sách của lượt index + thời gian ghi DB/map profile cuối lượt.
    return int(settings.board_index_budget_seconds) + 900


async def index_board(ctx: WorkerContext, *, source: str) -> dict[str, Any]:
    """Index tăng dần một trang tuyển dụng."""
    res = ctx["resources"]
    [adapter] = boards.selected_boards(res.settings, [source])
    stats = await boards.index_board(res, adapter, boards.index_options(res.settings))
    return dataclasses.asdict(stats)


async def index_boards_cron(ctx: WorkerContext) -> dict[str, Any]:
    """Cron: đưa mỗi trang tuyển dụng đang bật thành một job `index_board` riêng (key theo
    trang nên không nạp trùng khi lượt trước chưa xong)."""
    settings = ctx["settings"]
    queue = ctx["worker"].queue
    enqueued: list[str] = []
    for adapter in boards.selected_boards(settings):
        job = await queue.enqueue(
            "index_board",
            source=adapter.name,
            key=f"board:{adapter.name}",
            timeout=board_job_timeout(settings),
        )
        if job is not None:
            enqueued.append(adapter.name)
    log.info("boards.cron", enqueued=enqueued)
    return {"enqueued": enqueued}


def cron_jobs(settings: Settings) -> list[CronJob[WorkerContext]]:
    return [CronJob(index_boards_cron, cron=settings.board_index_cron, timeout=60)]


def build_worker(settings: Settings) -> Worker[WorkerContext]:
    queue = Queue.from_url(settings.redis_url, name=settings.queue_name)
    worker: Worker[WorkerContext] = Worker(
        queue,
        functions=[process_company, index_board],
        concurrency=settings.worker_concurrency,
        cron_jobs=cron_jobs(settings),
        startup=startup,
        shutdown=shutdown,
    )
    worker.context["settings"] = settings
    return worker


async def run_worker(settings: Settings) -> None:
    worker = build_worker(settings)
    await worker.queue.connect()
    try:
        await worker.start()
    finally:
        await worker.queue.disconnect()
