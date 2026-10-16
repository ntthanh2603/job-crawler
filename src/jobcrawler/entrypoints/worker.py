"""Cấu hình worker SAQ: functions, cron jobs, startup/shutdown dùng `open_resources`."""

from __future__ import annotations

from contextlib import AsyncExitStack
from typing import Any, Required

from saq import CronJob, Queue, Worker
from saq.types import Context

from jobcrawler.config import Settings
from jobcrawler.logging import get_logger
from jobcrawler.resources import Resources, open_resources
from jobcrawler.schemas import CompanyIn
from jobcrawler.services import companies

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


CRON_JOBS: list[CronJob[WorkerContext]] = []  # M4: index các trang tuyển dụng


def build_worker(settings: Settings) -> Worker[WorkerContext]:
    queue = Queue.from_url(settings.redis_url, name=settings.queue_name)
    worker: Worker[WorkerContext] = Worker(
        queue,
        functions=[process_company],
        concurrency=settings.worker_concurrency,
        cron_jobs=CRON_JOBS,
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
