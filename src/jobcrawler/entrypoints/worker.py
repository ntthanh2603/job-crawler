"""Cấu hình worker SAQ: functions, cron jobs, startup/shutdown dùng `open_resources`.

Hai vai trò, mỗi vai một queue:
- `companies` (`jobcrawler worker`): job `process_company` (≤ COMPANY_TIMEOUT_SECONDS), cron
  bảo trì và cron tính sẵn số liệu trang tổng quan; chạy nhiều replica, mỗi replica
  WORKER_CONCURRENCY doanh nghiệp.
- `boards` (`jobcrawler worker --boards`): job `index_board` (hàng giờ mỗi trang) và cron đưa các
  trang vào queue; tách riêng để lượt index không chiếm chỗ của doanh nghiệp.
"""

from __future__ import annotations

import dataclasses
from contextlib import AsyncExitStack
from typing import Any, Literal, Required

from saq import CronJob, Queue, Worker
from saq.types import Context

from jobcrawler.config import Settings
from jobcrawler.logging import get_logger
from jobcrawler.resources import Resources, open_resources
from jobcrawler.schemas import CompanyIn
from jobcrawler.services import boards, companies, maintenance, overview

log = get_logger(__name__)

WorkerRole = Literal["companies", "boards"]


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
    company_id: int | None = None,
) -> dict[str, Any]:
    company_in = CompanyIn(name=name, tax_code=tax_code, website=website, note=note)
    result = await companies.process_company(ctx["resources"], company_in, company_id=company_id)
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


async def maintenance_cron(ctx: WorkerContext) -> dict[str, Any]:
    """Cron: dọn dữ liệu cũ, đánh dấu lượt chạy treo, huấn luyện từ điển nén + nén lại."""
    report = await maintenance.run_maintenance(ctx["resources"])
    return report.as_dict()


async def overview_cron(ctx: WorkerContext) -> dict[str, Any]:
    """Cron: tính sẵn số liệu phân bố cho trang tổng quan (người xem chỉ đọc từ Redis)."""
    charts = await overview.refresh_charts(ctx["resources"])
    return charts["totals"]


# Bảo trì có thể phải nén lại hàng chục nghìn dòng.
MAINTENANCE_TIMEOUT = 3600
CANCEL_GRACE_SECONDS = 10.0


def cron_jobs(settings: Settings, role: WorkerRole = "companies") -> list[CronJob[WorkerContext]]:
    # Cron SAQ có khoá duy nhất theo hàm: nhiều replica cũng chỉ một lần mỗi mốc.
    if role == "boards":
        return [CronJob(index_boards_cron, cron=settings.board_index_cron, timeout=60)]
    return [
        CronJob(maintenance_cron, cron=settings.maintenance_cron, timeout=MAINTENANCE_TIMEOUT),
        CronJob(overview_cron, cron=settings.overview_cron, timeout=300),
    ]


def queue_name(settings: Settings, role: WorkerRole = "companies") -> str:
    return settings.board_queue_name if role == "boards" else settings.queue_name


def build_worker(settings: Settings, role: WorkerRole = "companies") -> Worker[WorkerContext]:
    queue = Queue.from_url(settings.redis_url, name=queue_name(settings, role))
    boards_role = role == "boards"
    worker: Worker[WorkerContext] = Worker(
        queue,
        functions=[index_board] if boards_role else [process_company],
        concurrency=settings.board_worker_concurrency
        if boards_role
        else settings.worker_concurrency,
        cron_jobs=cron_jobs(settings, role),
        # Job bị huỷ (timeout cứng, worker dừng) còn chừng này giây để ghi `company_runs`.
        cancellation_hard_deadline_s=CANCEL_GRACE_SECONDS,
        startup=startup,
        shutdown=shutdown,
    )
    worker.context["settings"] = settings
    return worker


async def run_worker(settings: Settings, role: WorkerRole = "companies") -> None:
    worker = build_worker(settings, role)
    await worker.queue.connect()
    try:
        await worker.start()
    finally:
        await worker.queue.disconnect()
