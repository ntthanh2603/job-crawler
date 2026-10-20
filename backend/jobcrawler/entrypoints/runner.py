"""Runner: chạy job trong các hàng đợi Redis Streams (`streamqueue.StreamWorker`).

Một process runner chạy một hoặc nhiều vai (mỗi vai một queue, một StreamWorker, dùng chung một
`Resources`):

- `build` (queue BUILD_QUEUE_NAME): `build_company` — lượt đủ, ghi bộ khung (đêm, khi poll lỗi,
  khi người dùng chạy); cron bảo trì và cron số liệu trang tổng quan.
- `poll` (queue POLL_QUEUE_NAME): `poll_company` — lượt nhẹ theo bộ khung (ngày), cần nhanh nên
  có queue riêng, không bao giờ phải chờ sau lượt build.
- `boards` (queue BOARD_QUEUE_NAME): `index_board` (đủ, đêm) và `tail_board` (đuôi, ngày).

Mạng không đi thẳng từ đây: mọi request tới website / ATS / trang tuyển dụng được gửi tới worker
lấy trang (gói jobworker) qua gateway gRPC.

Health: vòng lặp nhận việc của mỗi vai ghi heartbeat của process (`jobcrawler.heartbeat`; component
`runner-build` / `runner-poll` / `runner-boards`, đủ ba vai là `runner`), kiểm bằng
`jobcrawler health <component>` trong container.

Watchdog (`jobcrawler.watchdog`, WATCHDOG_SECONDS): event loop, hoặc vòng lặp nhận việc của BẤT KỲ
vai nào, đứng quá ngưỡng thì process thoát (mã 70) để Docker dựng lại. Mọi vòng đều tính (kể cả khi
đọc Redis lỗi): mất Redis / Postgres không làm thoát.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Sequence
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal, Required, TypedDict
from uuid import UUID
from zoneinfo import ZoneInfo

from jobcrawler import sysres
from jobcrawler.config import Settings
from jobcrawler.heartbeat import Heartbeat, runner_component
from jobcrawler.logging import get_logger
from jobcrawler.queues import make_queue
from jobcrawler.resources import Resources, open_resources
from jobcrawler.schemas import RunTrigger
from jobcrawler.services import boards, crawl, maintenance, overview
from jobcrawler.streamqueue import CronJob, StreamWorker
from jobcrawler.watchdog import start_watchdog

log = get_logger(__name__)

Role = Literal["build", "poll", "boards"]
ROLES: tuple[Role, ...] = ("build", "poll", "boards")
# Bảo trì có thể phải nén lại hàng chục nghìn dòng.
MAINTENANCE_TIMEOUT = 3600
CANCEL_GRACE_SECONDS = 10.0


class RunnerContext(TypedDict, total=False):
    worker: Required[StreamWorker]
    job: Any
    settings: Required[Settings]
    resources: Required[Resources]


def _as_uuid(value: object) -> UUID | None:
    try:
        return UUID(str(value)) if value is not None else None
    except ValueError:
        return None


def job_ready_at(job: Any) -> datetime | None:
    """Lúc job sẵn sàng chạy: lúc xếp hàng (ms epoch), hoặc giờ hẹn (giây epoch) nếu muộn hơn —
    lượt hẹn giờ không tính phần chờ tới giờ hẹn là thời gian chờ."""
    queued = getattr(job, "queued", 0) or 0
    if not queued:
        return None
    return datetime.fromtimestamp(max(queued / 1000, getattr(job, "scheduled", 0) or 0), UTC)


async def build_company(
    ctx: RunnerContext,
    *,
    company_id: str,
    trigger: str = RunTrigger.MANUAL.value,
    continuation: int = 0,
    refresh: bool | None = None,
) -> dict[str, Any]:
    """Lượt build một doanh nghiệp (đã có trong `companies`). `refresh`: tìm lại nguồn trên
    website (None: theo lý do xếp lượt)."""
    res = ctx["resources"]
    cid = _as_uuid(company_id)
    if cid is None:
        return {"error": f"company_id không hợp lệ: {company_id!r}"}
    result = await crawl.build_company(
        res,
        cid,
        trigger=RunTrigger(trigger),
        continuation=continuation,
        queued_at=job_ready_at(ctx.get("job")),
        refresh=refresh,
    )
    return result.model_dump(mode="json", exclude={"jobs"})


async def poll_company(
    ctx: RunnerContext, *, company_id: str, trigger: str = RunTrigger.SCHEDULE.value
) -> dict[str, Any]:
    """Lượt poll một doanh nghiệp theo bộ khung."""
    res = ctx["resources"]
    cid = _as_uuid(company_id)
    if cid is None:
        return {"error": f"company_id không hợp lệ: {company_id!r}"}
    outcome = await crawl.poll_company(
        res, cid, trigger=RunTrigger(trigger), queued_at=job_ready_at(ctx.get("job"))
    )
    return outcome.as_dict()


def board_job_timeout(settings: Settings) -> int:
    # Ngân sách của lượt index + thời gian ghi DB/map profile cuối lượt.
    return int(settings.board_index_budget_seconds) + 900


def board_tail_timeout(settings: Settings) -> int:
    return int(settings.board_tail_budget_seconds) + 120


async def _poll_companies_with_new_jobs(res: Resources, source: str, since: datetime) -> int:
    if not res.settings.board_poll_companies:
        return 0
    company_ids = await boards.companies_with_new_postings(res, source, since)
    return await crawl.enqueue_polls(res, company_ids, trigger=RunTrigger.BOARD)


async def index_board(ctx: RunnerContext, *, source: str) -> dict[str, Any]:
    """Index đủ một trang tuyển dụng, rồi poll ngay doanh nghiệp có profile vừa có tin mới."""
    res = ctx["resources"]
    [adapter] = boards.selected_boards(res.settings, [source])
    started = datetime.now(UTC)
    stats = await boards.index_board(res, adapter, boards.index_options(res.settings))
    out = dataclasses.asdict(stats)
    out["companies_polled"] = await _poll_companies_with_new_jobs(res, adapter.name, started)
    return out


async def tail_board(ctx: RunnerContext, *, source: str) -> dict[str, Any]:
    """Index đuôi (phần mới nhất) một trang tuyển dụng, rồi poll doanh nghiệp có tin mới."""
    res = ctx["resources"]
    [adapter] = boards.selected_boards(res.settings, [source])
    started = datetime.now(UTC)
    stats = await boards.tail_board(res, adapter)
    out = dataclasses.asdict(stats)
    out["companies_polled"] = await _poll_companies_with_new_jobs(res, adapter.name, started)
    return out


async def _enqueue_boards(ctx: RunnerContext, function: str, timeout: int) -> dict[str, Any]:
    settings = ctx["resources"].settings
    queue = ctx["worker"].queue
    enqueued: list[str] = []
    for adapter in boards.selected_boards(settings):
        job = await queue.enqueue(
            function, source=adapter.name, key=f"board:{adapter.name}", timeout=timeout
        )
        if job is not None:
            enqueued.append(adapter.name)
    log.info("boards.cron", function=function, enqueued=enqueued)
    return {"enqueued": enqueued}


async def index_boards_cron(ctx: RunnerContext) -> dict[str, Any]:
    """Cron (đêm): mỗi trang tuyển dụng một job `index_board` (khoá theo trang nên không trùng
    với lượt trước chưa xong, kể cả lượt đuôi)."""
    return await _enqueue_boards(ctx, "index_board", board_job_timeout(ctx["resources"].settings))


async def tail_boards_cron(ctx: RunnerContext) -> dict[str, Any]:
    """Cron (ngày): mỗi trang tuyển dụng một job `tail_board`."""
    return await _enqueue_boards(ctx, "tail_board", board_tail_timeout(ctx["resources"].settings))


async def maintenance_cron(ctx: RunnerContext) -> dict[str, Any]:
    """Cron: dọn dữ liệu cũ, đánh dấu lượt chạy treo, huấn luyện từ điển nén + nén lại."""
    report = await maintenance.run_maintenance(ctx["resources"])
    return report.as_dict()


async def overview_cron(ctx: RunnerContext) -> dict[str, Any]:
    """Cron: tính sẵn số liệu phân bố cho trang tổng quan (người xem chỉ đọc từ Redis)."""
    charts = await overview.refresh_charts(ctx["resources"])
    return charts["totals"]


def cron_jobs(settings: Settings, role: Role) -> list[CronJob]:
    # Cron có khoá duy nhất theo hàm: nhiều replica cũng chỉ một lần mỗi mốc.
    if role == "boards":
        return [
            CronJob(index_boards_cron, cron=settings.board_index_cron, timeout=60),
            CronJob(tail_boards_cron, cron=settings.board_tail_cron, timeout=60),
        ]
    if role == "build":
        return [
            CronJob(maintenance_cron, cron=settings.maintenance_cron, timeout=MAINTENANCE_TIMEOUT),
            CronJob(overview_cron, cron=settings.overview_cron, timeout=300),
        ]
    return []


def queue_name(settings: Settings, role: Role) -> str:
    build, poll, boards_ = settings.queue_names
    return {"build": build, "poll": poll, "boards": boards_}[role]


def concurrency(settings: Settings, role: Role) -> int:
    """Số job song song của vai: cài đặt, 0 = tự tính theo máy (`jobcrawler.sysres`)."""
    value = {
        "build": settings.runner_build_concurrency,
        "poll": settings.runner_poll_concurrency,
        "boards": settings.runner_board_concurrency,
    }[role]
    return value or sysres.auto_concurrency(role)


FUNCTIONS: dict[Role, list[Any]] = {
    "build": [build_company],
    "poll": [poll_company],
    "boards": [index_board, tail_board],
}


def build_worker(settings: Settings, role: Role) -> StreamWorker:
    worker = StreamWorker(
        make_queue(settings, queue_name(settings, role)),
        functions=FUNCTIONS[role],
        concurrency=concurrency(settings, role),
        cron_jobs=cron_jobs(settings, role),
        # Job bị huỷ (timeout cứng, runner dừng) còn chừng này giây để ghi `company_runs`.
        cancellation_hard_deadline_s=CANCEL_GRACE_SECONDS,
        handle_signals=False,
        tz=ZoneInfo(settings.crawl_timezone),
    )
    worker.context["settings"] = settings
    return worker


async def run_runner(settings: Settings, roles: Sequence[Role] = ROLES) -> None:
    """Chạy các vai `roles` trong một process, dùng chung một `Resources`. Dừng bằng SIGTERM /
    Ctrl+C: job đang chạy được làm nốt (hoặc xếp lại) rồi thoát."""
    roles = list(dict.fromkeys(roles))
    async with open_resources(settings) as res:
        # Heartbeat health: chỉ ghi khi vòng lặp của mọi vai đều đang quay.
        beat = Heartbeat(res.redis, res.settings.redis_prefix, runner_component(roles), parts=roles)
        # Số job cùng lúc và lịch cron theo cài đặt hiện hành (kể cả giá trị sửa ở trang Cài đặt).
        workers = [build_worker(res.settings, role) for role in roles]
        for role, worker in zip(roles, workers, strict=True):
            worker.context["resources"] = res
            worker.on_loop = partial(beat.tick, role)
            await worker.queue.connect()
        log.info(
            "runner.start",
            roles=roles,
            component=beat.component,
            concurrency={w.queue.name: w.concurrency for w in workers},
        )
        # Watchdog: mỗi vai một vòng lặp chính, vai nào đứng cũng làm process thoát.
        dog = start_watchdog(beat.component, max_stall=res.settings.watchdog_seconds, parts=roles)

        def shutdown() -> None:
            dog.stop()  # dừng có chủ đích: lượt làm nốt job không bị watchdog giết
            for worker in workers:
                worker.stop()

        loop = asyncio.get_running_loop()
        signals = StreamWorker.SIGNALS
        try:
            for role, worker in zip(roles, workers, strict=True):
                worker.on_iteration = partial(dog.loop_iteration, role)
            for signum in signals:
                loop.add_signal_handler(signum, shutdown)
            await asyncio.gather(*(w.start() for w in workers))
        finally:
            dog.stop()
            for signum in signals:
                loop.remove_signal_handler(signum)
            for worker in workers:
                worker.stop()
                await worker.queue.disconnect()
