"""Build (đủ, ghi bộ khung) và poll (theo bộ khung) một doanh nghiệp; xếp job vào queue.

Build (`build_company`) là lượt `process_company` đầy đủ (modules/crawl/company_service.py) kèm ghi
bộ khung và lịch; lượt `partial` tự xếp lượt chạy tiếp.

Poll (`poll_company`) — ban ngày, mỗi POLL_INTERVAL_SECONDS:
1. Nạp bộ khung các nguồn website / ATS và (mã, URL) các tin đã lưu — không đọc nội dung tin.
2. Thu thập ở chế độ poll: nguồn chỉ đọc trang danh sách / API trong bộ khung, không dùng HTTP
   cache, tin đã biết được dùng lại (không mở trang chi tiết), chỉ mở trang của link mới.
3. So tập mã tin với lượt trước (`crawl_plans.job_keys`). Không đổi → chỉ cập nhật lịch: không
   ghi `company_runs`, không đụng `jobs` (đa số lượt poll đi đường này).
4. Có tin mới / tin biến mất (nguồn đọc trọn) / trang tuyển dụng báo có tin mới (lượt `board` hoặc
   cờ tin mới, xem `mark_board_pending`) → chỉ tin mới được chuẩn hoá → xác minh → gộp (với việc
   đã có, cùng các tin khác của việc đó: `store.save_company_jobs`) → lưu; tin không còn thấy được
   đánh `removed` và việc của nó đóng nếu không còn tin nào khác. Chi phí một lượt poll theo số tin
   MỚI, không theo số tin của doanh nghiệp. Ghi `company_runs` (kind=poll).
5. Bộ khung không còn đúng (trang chính 404/410, API không còn trả tin, trang danh sách trống, lỗi
   tạm lặp lại POLL_MAX_FAILURES lần) → nguồn `broken`, doanh nghiệp được build lại ngay
   (REBUILD_ON_ERROR tắt: chờ build đêm). Doanh nghiệp `paused` không bao giờ đổi trạng thái lịch.

Build và poll cùng một doanh nghiệp không chạy chồng nhau (khoá Redis theo doanh nghiệp, tự gia
hạn); lượt đã mất khoá không ghi được DB nữa (fencing token, `database/fencing.py`).

Truy vấn bộ khung và lịch thu thập của một doanh nghiệp cho giao diện.

Thao tác thu thập cho API: build / poll ngay, số đo độ trễ phát hiện."""

from __future__ import annotations

import dataclasses
import re
import time
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from math import ceil
from typing import Any
from uuid import UUID

import structlog
from pydantic import ValidationError
from sqlalchemy import insert, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from jobcrawler.common.config import Settings
from jobcrawler.common.logging import get_logger
from jobcrawler.common.schemas import (
    CompanyIn,
    CompanyResult,
    FetchOutcome,
    JobStatus,
    KycStatus,
    PlanStatus,
    RawJob,
    RunKind,
    RunStatus,
    RunTrigger,
    ScheduleState,
)
from jobcrawler.database import fencing
from jobcrawler.database.ids import uuid7
from jobcrawler.database.models import (
    Company,
    CompanyRun,
    CompanySchedule,
    CrawlPlan,
    FailedPage,
    Job,
    JobPosting,
)
from jobcrawler.modules.boards import boards_service
from jobcrawler.modules.crawl import company_service, plan_service, schedule_service, sla_service
from jobcrawler.modules.crawl.crawl_schema import (
    PlanApiOut,
    PlanDetailOut,
    PlanJobOut,
    PlanNameOut,
    PlanOut,
    PlanPageOut,
    PlanPollResult,
    PollLimitsOut,
)
from jobcrawler.modules.gateway.dispatch_service import priority_fetches
from jobcrawler.pipeline import store
from jobcrawler.pipeline.collect import CollectResult, SourceReport, collect
from jobcrawler.services.redis.locks import RedisLease
from jobcrawler.services.resources import Resources
from jobcrawler.sources.base import (
    FetchRecord,
    KnownJobs,
    Source,
    SourceLimits,
    failed_page_rows,
    is_stub,
)
from jobcrawler.sources.boards import registry as board_registry
from jobcrawler.sources.plan import ApiSpec, PlanSpec, load_spec

log = get_logger(__name__)

# Lỗi trang chính không tự hết: bộ khung hỏng ngay, không chờ đủ POLL_MAX_FAILURES lượt.
_GONE = re.compile(r"\bhttp_(404|410)\b")
# Phần cuối timeout job poll dành cho việc lưu.
POLL_SAVE_MARGIN = 15.0
# Chốt chặn đóng nhầm: mất từ 30% tin (và ít nhất 3 tin) của nguồn có từ 5 tin trong một lượt.
SUSPICIOUS_MIN_JOBS = 5
SUSPICIOUS_SHARE = 0.3


def poll_limits(s: Settings) -> SourceLimits:
    """Giới hạn nguồn của lượt poll: như lượt build nhưng ít trang chi tiết hơn để lượt poll luôn
    nhanh; tin mới còn lại được mở ở lượt poll sau (lượt này không đủ nên không đóng việc nào).
    Số trang danh sách / API theo bộ khung của từng nguồn (`collect` thay bằng `PlanSpec.pages`)."""
    return dataclasses.replace(
        SourceLimits.from_settings(s),
        max_detail_pages=s.poll_max_detail_pages,
        poll_pages=s.plan_max_pages,
    )


def build_key(company_id: UUID) -> str:
    return f"build:{company_id}"


def poll_key(company_id: UUID) -> str:
    return f"poll:{company_id}"


# --- tin mới trên trang tuyển dụng chờ poll ------------------------------------------------------

# Job poll có một khoá `poll:<id>` cho mọi lý do, nên lượt poll `board` (index trang tuyển dụng vừa
# thấy tin mới của doanh nghiệp) bị bỏ khi đã có lượt poll đang chờ / chạy. Để tin không phải chờ
# tới build đêm: xếp poll `board` luôn đặt cờ Redis (trạng thái, `res.redis`); lượt poll giữ khoá
# doanh nghiệp lấy và xoá cờ trước khi đọc index, có cờ thì đọc tin trang tuyển dụng như lượt
# `board`. Cờ đặt trong lúc lượt build / poll đang chạy (đã qua bước đọc cờ, hoặc lượt `board` gặp
# khoá nên bỏ lượt) được thấy lúc lượt đó xong: xếp thêm một lượt poll `board`. Hết hạn sau một
# ngày (build đêm đọc lại mọi tin).
BOARD_PENDING_TTL_SECONDS = 24 * 3600


def _board_pending_key(res: Resources, company_id: UUID) -> str:
    return f"{res.settings.redis_prefix}:poll:board_pending:{company_id}"


async def mark_board_pending(res: Resources, company_ids: Iterable[UUID]) -> None:
    """Đánh dấu doanh nghiệp có tin mới trên trang tuyển dụng chờ lượt poll đọc."""
    keys = [_board_pending_key(res, cid) for cid in company_ids]
    if not keys:
        return
    async with res.redis.pipeline(transaction=False) as pipe:
        for key in keys:
            pipe.set(key, 1, ex=BOARD_PENDING_TTL_SECONDS)
        await pipe.execute()


async def take_board_pending(res: Resources, company_id: UUID) -> bool:
    """Lấy và xoá cờ tin mới trên trang tuyển dụng (lượt poll gọi khi đã giữ khoá doanh nghiệp)."""
    return await res.redis.getdel(_board_pending_key(res, company_id)) is not None


async def follow_up_board_poll(res: Resources, company_id: UUID) -> bool:
    """Lượt build / poll vừa nhả khoá: còn cờ tin mới trên trang tuyển dụng (đặt trong lúc lượt đó
    chạy) thì xếp lượt poll `board`. Khoá `poll:<id>` có thể vẫn là job đang chạy này nên thử thêm
    khoá `poll:<id>:board`. True nếu đã xếp. Lỗi Redis chỉ ghi log: lượt vừa xong đã lưu, cờ còn
    đó cho lượt poll sau."""
    try:
        if not await res.redis.exists(_board_pending_key(res, company_id)):
            return False
        for key in (poll_key(company_id), f"{poll_key(company_id)}:board"):
            if await _enqueue_poll(res, key, company_id, RunTrigger.BOARD):
                log.info("poll.board_follow_up", company_id=str(company_id), key=key)
                return True
    except Exception as exc:
        log.warning("poll.board_follow_up_error", company_id=str(company_id), error=str(exc))
    return False


# --- khoá theo doanh nghiệp --------------------------------------------------------------------

# Khoá tự gia hạn mỗi ttl/3 khi đang giữ: runner chết thì khoá tự hết hạn sau chừng này giây.
COMPANY_LOCK_TTL_SECONDS = 60.0
# Build chờ lượt poll đang chạy thêm chừng này giây ngoài POLL_TIMEOUT_SECONDS (job poll bị huỷ ở
# timeout + CANCEL_GRACE_SECONDS của runner, lúc đó khoá được nhả).
BUILD_LOCK_EXTRA_WAIT_SECONDS = 15.0


class CompanyBusyError(RuntimeError):
    """Build không giữ được khoá doanh nghiệp trong thời gian chờ: lượt khác đang chạy."""


@asynccontextmanager
async def company_lock(
    res: Resources,
    company_id: UUID,
    *,
    ttl: float = COMPANY_LOCK_TTL_SECONDS,
    wait: float = 0,
) -> AsyncIterator[bool]:
    """Khoá Redis theo doanh nghiệp (build / poll không chạy chồng). Trả True nếu giữ được khoá
    trong `wait` giây. Đang giữ thì khoá tự gia hạn; mất khoá giữa chừng (process đứng quá `ttl`)
    thì khối bị huỷ và ném `LeaseLost`. Mọi commit DB trong khối kèm fencing token của lượt giữ
    khoá (`database/fencing.py`): lượt cũ đã mất khoá không ghi đè được dữ liệu của lượt mới."""
    key = f"{res.settings.redis_prefix}:lock:company:{company_id}"
    async with RedisLease(res.redis, key, ttl=ttl).hold(wait=wait) as held:
        if not held:
            yield False
            return
        token = await fencing.next_token(res.sessionmaker, company_id)
        with fencing.fenced(company_id, token):
            yield True


# --- build ---------------------------------------------------------------------------------------


async def build_company(
    res: Resources,
    company_id: UUID,
    *,
    trigger: RunTrigger = RunTrigger.MANUAL,
    continuation: int = 0,
    queued_at: datetime | None = None,
    continue_partial: bool = True,
    refresh: bool | None = None,
) -> CompanyResult:
    """Lượt build một doanh nghiệp đã có trong `companies`; `partial` thì xếp lượt chạy tiếp
    (trừ khi `continue_partial=False`)."""
    s = res.settings
    # Timeout cứng của job tính từ lúc job bắt đầu: thời gian chờ khoá trừ vào hạn của lượt build.
    deadline = time.monotonic() + s.company_deadline_seconds
    async with res.sessionmaker() as session:
        company = await session.get(Company, company_id)
    if company is None:
        raise LookupError(f"không có doanh nghiệp {company_id}")
    company_in = CompanyIn(
        name=company.input_name,
        tax_code=company.tax_code,
        website=company.input_website,
        note=company.note,
    )
    # Build đêm và build lại khi bộ khung hỏng: tìm nguồn lại từ đầu. Lượt chạy tiếp dùng cache
    # tìm nguồn của lượt đầu.
    if refresh is None:
        refresh = (
            s.build_refresh_discovery
            and continuation == 0
            and trigger in (RunTrigger.NIGHTLY, RunTrigger.ERROR)
        )
    # Chờ lượt poll đang chạy (nếu có) xong. Không giữ được khoá (lượt build khác đang chạy) thì
    # không chạy chồng: `CompanyBusyError`, lượt chạy job ghi `skipped`.
    async with company_lock(
        res, company_id, wait=s.poll_timeout_seconds + BUILD_LOCK_EXTRA_WAIT_SECONDS
    ) as held:
        if not held:
            raise CompanyBusyError(f"doanh nghiệp {company_id} đang có lượt build / poll khác")
        result = await company_service.process_company(
            res,
            company_in,
            company_id=company_id,
            queued_at=queued_at,
            trigger=trigger,
            refresh_discovery=refresh,
            deadline=deadline,
        )
    await follow_up_board_poll(res, company_id)
    if (
        continue_partial
        and result.status is RunStatus.PARTIAL
        and continuation < s.company_max_continuations
    ):
        # Chạm giới hạn / hết giờ / tin chờ LLM: lượt sau dùng lại phần đã tải và đi tiếp.
        job = await res.queues.build.enqueue(
            "build_company",
            key=f"{build_key(result.company_id)}:cont{continuation + 1}",
            timeout=s.company_timeout_seconds,
            retries=s.company_job_retries,
            scheduled=int(time.time() + s.company_continuation_delay_seconds),
            company_id=str(result.company_id),
            trigger=RunTrigger.CONTINUATION.value,
            continuation=continuation + 1,
        )
        log.info(
            "company.continue",
            company_id=result.company_id,
            continuation=continuation + 1,
            queued=job is not None,
            incomplete_sources=result.incomplete_sources,
        )
    return result


# --- poll ----------------------------------------------------------------------------------------


@dataclass(slots=True)
class PollOutcome:
    company_id: UUID
    status: str  # ok | partial | failed | broken | skipped | no_plans
    new: int = 0  # tin mới (mã chưa có ở lượt trước)
    gone: int = 0  # tin không còn trên nguồn đọc trọn
    closed: int = 0  # job bị đóng khi lưu
    saved: bool = False  # có ghi tin / job (lượt có thay đổi)
    duration_ms: int = 0
    rebuild: bool = False
    # Mọi nguồn đọc được danh sách: tin đăng trước lúc bắt đầu lượt này chắc chắn đã được thấy.
    fresh: bool = False
    # Cửa sổ phát hiện: lúc xong − lúc bắt đầu lượt poll đủ trước (modules/crawl/sla_service.py).
    window_s: float | None = None
    run_id: UUID | None = None
    error: str | None = None
    sources: dict[str, str] = field(default_factory=dict)  # nguồn → ok | lỗi

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["company_id"] = str(self.company_id)
        out["run_id"] = str(self.run_id) if self.run_id else None
        return out


@dataclass(slots=True)
class _PlanCheck:
    plan: CrawlPlan
    report: SourceReport | None
    keys: list[str]
    new: set[str]
    gone: set[str]
    ok: bool  # đọc được danh sách
    complete: bool  # đọc trọn: tin vắng mặt là tin đã gỡ
    broken: str | None
    error: str | None
    suspect: str | None = None  # đọc được nhưng kết quả đáng ngờ (vd. mất nhiều tin cùng lúc)


def suspicious_drop(previous: int, gone: int) -> bool:
    """Mất nhiều tin cùng lúc ở một lượt poll: nhiều khả năng lượt poll không thấy hết (bộ khung
    thiếu một nguồn dữ liệu của trang) hơn là doanh nghiệp vừa gỡ hàng loạt."""
    return previous >= SUSPICIOUS_MIN_JOBS and gone >= max(3, SUSPICIOUS_SHARE * previous)


def _check(plan: CrawlPlan, report: SourceReport | None, max_failures: int) -> _PlanCheck:
    if report is None:
        return _PlanCheck(
            plan, None, [], set(), set(), False, False, "bộ khung cũ / không dựng lại được", None
        )
    keys = list(dict.fromkeys(report.keys))
    previous = set(plan.job_keys or [])
    new = set(keys) - previous
    complete = report.complete
    # Tin gián tiếp (chỉ tới được qua trang tin khác): lượt poll không thấy lại thì vẫn giữ.
    indirect = previous & set((plan.spec or {}).get("indirect") or [])
    keys += sorted(indirect - set(keys))
    gone = previous - set(keys) if complete else set()
    error = report.error or (", ".join(report.truncated) if report.truncated else None)
    broken = report.broken
    ok = report.outcome is FetchOutcome.OK and broken is None
    suspect = None
    if complete and suspicious_drop(len(previous), len(gone)):
        # Không đóng việc theo lượt này; lặp lại đủ POLL_MAX_FAILURES lượt thì build lại để biết
        # chắc (lượt build đọc đủ và đóng đúng những việc đã gỡ).
        suspect = f"coverage_drop: {len(gone)}/{len(previous)} tin không còn thấy"
        complete, gone = False, set()
    failing = not ok or suspect is not None
    if broken is None and failing:
        if report.error and _GONE.search(report.error):
            broken = report.error
        elif plan.failures + 1 >= max_failures:
            broken = f"lỗi {plan.failures + 1} lượt liên tiếp: {suspect or report.error}"
    return _PlanCheck(plan, report, keys, new, gone, ok, complete, broken, error, suspect)


async def _posting_refs(
    res: Resources, company_id: UUID, kinds: set[str]
) -> list[tuple[str, str, str]]:
    """(nguồn, mã, URL) các tin đã lưu của doanh nghiệp từ các loại nguồn `kinds` (mọi trạng
    thái: tin đã đóng mà đăng lại thì vẫn biết là tin cũ)."""
    if not kinds:
        return []
    async with res.sessionmaker() as session:
        rows = await session.execute(
            select(JobPosting.source, JobPosting.external_id, JobPosting.url).where(
                JobPosting.company_id == company_id, JobPosting.source.in_(kinds)
            )
        )
        return [(source, ext, url) for source, ext, url in rows]


async def _hydrate(res: Resources, jobs: list[RawJob]) -> tuple[list[RawJob], int]:
    """Thay tin dùng lại (chỉ có mã) bằng nội dung đã lưu. Trả (tin, số tin không còn nội dung)."""
    stubs = {(j.source, j.external_id) for j in jobs if is_stub(j)}
    if not stubs:
        return jobs, 0
    async with res.sessionmaker() as session:
        rows = await session.execute(
            select(JobPosting.source, JobPosting.external_id, JobPosting.raw).where(
                tuple_(JobPosting.source, JobPosting.external_id).in_(list(stubs))
            )
        )
        stored = {(source, ext): RawJob.model_validate(raw) for source, ext, raw in rows}
    out: list[RawJob] = []
    missing = 0
    for job in jobs:
        if not is_stub(job):
            out.append(job)
        elif (full := stored.get((job.source, job.external_id))) is not None:
            out.append(full)
        else:
            missing += 1
    return out, missing


async def _board_jobs(
    res: Resources, company: Company, records: list[FetchRecord], deadline: float
) -> CollectResult:
    """Tin của doanh nghiệp trên các trang tuyển dụng, chỉ từ index chung (không gọi mạng)."""
    sources = await boards_service.board_sources(
        res,
        company.id,
        policy=company_service.profile_policy(
            None if company.kyc_status is None else KycStatus(company.kyc_status),
            name_profiles=res.settings.board_name_profiles_without_kyc,
        ),
        names=company_service.company_names(company),
        network=False,
    )
    return await collect(
        list(sources),
        fetcher=res.http,
        llm=res.llm,
        deadline=deadline,
        records=records,
        mode="poll",
    )


async def _save_run(
    res: Resources,
    run: CompanyRun,
    records: list[FetchRecord],
) -> None:
    async with res.sessionmaker.begin() as session:
        session.add(run)
        await session.flush()
        await company_service.insert_fetches(session, run.run_id, run.company_id, records)
        failed = failed_page_rows(
            records, limit=res.settings.failed_pages_per_run, run_id=run.run_id
        )
        if failed:
            await session.execute(insert(FailedPage), failed)


async def poll_company(
    res: Resources,
    company_id: UUID,
    *,
    trigger: RunTrigger = RunTrigger.SCHEDULE,
    queued_at: datetime | None = None,
) -> PollOutcome:
    s = res.settings
    t0 = time.monotonic()
    started_at = datetime.now(UTC)
    outcome = PollOutcome(company_id=company_id, status="skipped")
    async with company_lock(res, company_id) as held:
        if not held:
            # Lượt đang giữ khoá xếp lượt poll `board` khi xong nếu còn cờ tin mới.
            outcome.error = "doanh nghiệp đang được build"
            return outcome
        # Đọc cờ trước khi đọc index: tin index thấy sau lúc này đặt cờ mới cho lượt sau.
        board = await take_board_pending(res, company_id) or trigger is RunTrigger.BOARD
        # Việc lấy trang của lượt poll đi stream ưu tiên: không xếp sau việc build (SLA phát hiện).
        with (
            structlog.contextvars.bound_contextvars(company_id=str(company_id), poll=True),
            priority_fetches(),
        ):
            try:
                outcome = await _poll(res, company_id, trigger, board, queued_at, started_at, t0)
            except fencing.FencedOut:
                if board:  # lượt đang giữ khoá đọc lại
                    await mark_board_pending(res, [company_id])
                raise  # lượt khác đã giữ khoá: không ghi gì thêm
            except Exception as exc:
                log.exception("poll.error")
                outcome = PollOutcome(
                    company_id=company_id, status="failed", error=f"{type(exc).__name__}: {exc}"
                )
                async with res.sessionmaker.begin() as session:
                    await session.execute(
                        update(CompanySchedule)
                        .where(CompanySchedule.company_id == company_id)
                        .values(
                            last_poll_at=started_at,
                            poll_failures=CompanySchedule.poll_failures + 1,
                            last_error=(outcome.error or "")[:500],
                        )
                    )
                if board:
                    # Lượt poll theo lịch kế tiếp đọc lại (không xếp lại ngay: lỗi lặp lại thì
                    # không thành vòng poll liên tục).
                    await mark_board_pending(res, [company_id])
    if outcome.status != "failed":
        await follow_up_board_poll(res, company_id)
    outcome.duration_ms = int((time.monotonic() - t0) * 1000)
    if outcome.fresh:
        metric = "ok"
    elif outcome.status in ("broken", "failed", "partial"):
        metric = "broken" if outcome.status == "broken" else "failed"
    else:
        metric = "skipped"
    await sla_service.record_poll(
        res.redis,
        s.redis_prefix,
        status=metric,
        duration_s=outcome.duration_ms / 1000,
        window_s=outcome.window_s,
        new_jobs=outcome.new,
        changed=outcome.saved,
    )
    return outcome


async def _poll(
    res: Resources,
    company_id: UUID,
    trigger: RunTrigger,
    board: bool,
    queued_at: datetime | None,
    started_at: datetime,
    t0: float,
) -> PollOutcome:
    """`board`: đọc thêm tin của doanh nghiệp trên các trang tuyển dụng (index vừa thấy tin mới)."""
    s = res.settings
    outcome = PollOutcome(company_id=company_id, status="ok")
    async with res.sessionmaker() as session:
        company = await session.get(Company, company_id)
        schedule = await session.get(CompanySchedule, company_id)
        plans = await plan_service.active_plans(session, company_id)
        # Nguồn website / ATS đang hỏng (chờ build lại): lượt này không đủ để đóng việc cũ.
        unreadable = await plan_service.has_broken_plans(session, company_id)
    if company is None:
        outcome.status, outcome.error = "skipped", "không còn doanh nghiệp"
        return outcome
    # Doanh nghiệp đã tắt thu thập: chỉ poll khi người dùng bấm, và không bao giờ đổi trạng thái
    # lịch (lượt poll `board` / lượt còn trong hàng đợi từ trước khi tắt thì bỏ).
    paused = schedule is not None and schedule.state == ScheduleState.PAUSED.value
    if paused and trigger is not RunTrigger.MANUAL:
        outcome.status, outcome.error = "skipped", "doanh nghiệp đã tắt thu thập"
        return outcome
    if not plans and not board:
        outcome.status = "no_plans"
        return outcome
    if board and trigger is RunTrigger.SCHEDULE:
        trigger = RunTrigger.BOARD  # lượt theo lịch đọc cờ tin mới trên trang tuyển dụng

    sources: list[Source] = []
    specs: dict[str, PlanSpec] = {}
    by_name: dict[str, CrawlPlan] = {}
    for plan in plans:
        built = plan_service.plan_source(plan)
        by_name[plan.source] = plan
        if built is not None:
            source, spec = built
            sources.append(source)
            specs[source.name] = spec
    kinds = {plan.kind for plan in plans}
    known = KnownJobs.of_refs(await _posting_refs(res, company_id, kinds))
    records: list[FetchRecord] = []
    deadline = t0 + s.poll_deadline_seconds
    collected = await collect(
        sources,
        fetcher=res.http,
        llm=res.llm,
        deadline=deadline,
        limits=poll_limits(s),
        records=records,
        known=known,
        mode="poll",
        plans=specs,
    )
    reports = {r.name: r for r in collected.reports}
    checks = [_check(plan, reports.get(plan.source), s.poll_max_failures) for plan in plans]
    outcome.new = sum(len(c.new) for c in checks)
    outcome.gone = sum(len(c.gone) for c in checks)
    outcome.sources = {
        c.plan.source: "ok"
        if c.ok and not c.suspect
        else (c.broken or c.suspect or c.error or "lỗi")
        for c in checks
    }
    all_ok = all(c.ok for c in checks)
    broken = [c for c in checks if c.broken]
    changed = bool(outcome.new or outcome.gone) or board
    finished_at = datetime.now(UTC)

    run_id: UUID | None = None
    processed: company_service.ProcessedJobs | None = None
    if changed or not all_ok:
        run_id = uuid7()
        outcome.run_id = run_id
        incomplete = list(collected.incomplete_sources)
        if changed:
            # Chỉ xử lý tin mới (chuẩn hoá, xác minh, gộp với việc đã có) — không xử lý lại cả
            # danh sách mỗi lượt poll; tin đã gỡ được đóng riêng ngay sau đó.
            new_keys = {(c.plan.kind, ext) for c in checks for ext in c.new}
            fresh = [j for j in collected.jobs if (j.source, j.external_id) in new_keys]
            raws, _missing = await _hydrate(res, fresh)
            board_jobs: list[RawJob] = []
            if board:
                boards = await _board_jobs(res, company, records, deadline)
                incomplete += boards.incomplete_sources
                board_jobs = boards.jobs
            gone = {(c.plan.kind, ext) for c in checks for ext in c.gone}
            processed = await company_service.process_jobs(
                res,
                company,
                [*raws, *board_jobs],
                website=company.primary_domain,
                deadline=t0 + s.poll_timeout_seconds - POLL_SAVE_MARGIN,
                complete=False,
                reused=known.used,
                absent=gone,
            )
            closed = 0
            if gone and not unreadable:
                present = {(c.plan.kind, ext) for c in checks for ext in c.keys}
                async with res.sessionmaker.begin() as session:
                    closed = await store.close_postings(
                        session,
                        company_id,
                        gone,
                        present=present,
                        boards=set(board_registry()),
                        now=datetime.now(UTC),
                    )
            outcome.saved = True
            outcome.closed = processed.removed + closed
            if processed.deferred:
                incomplete.append(company_service.VERIFY_SOURCE)
        finished_at = datetime.now(UTC)
        status = (
            RunStatus.FAILED
            if not any(c.ok for c in checks) and checks
            else RunStatus.PARTIAL
            if incomplete or not all_ok
            else RunStatus.OK
        )
        outcome.status = status.value
        await _save_run(
            res,
            CompanyRun(
                run_id=run_id,
                company_id=company_id,
                kind=RunKind.POLL.value,
                trigger=trigger.value,
                queued_at=queued_at,
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=int((time.monotonic() - t0) * 1000),
                status=status.value,
                jobs_found=len(collected.jobs),
                jobs_accepted=len(processed.jobs) if processed else 0,
                jobs_new=processed.new if processed else 0,
                jobs_closed=outcome.closed,
                incomplete_sources=incomplete,
                error="; ".join(
                    f"{c.plan.source}: {c.broken or c.error}" for c in checks if not c.ok
                )[:2000]
                or None,
            ),
            records,
        )
    if broken:
        outcome.status = "broken"
        outcome.rebuild = s.rebuild_on_error and not paused
    elif not all_ok and outcome.status == "ok":
        outcome.status = "partial"

    # Bộ khung + lịch.
    async with res.sessionmaker.begin() as session:
        for c in checks:
            values: dict[str, Any] = {"polled_at": started_at}
            if c.broken:
                values |= {
                    "status": PlanStatus.BROKEN.value,
                    "failures": c.plan.failures + 1,
                    "last_error": c.broken[:500],
                }
            elif not c.ok or c.suspect:
                values |= {
                    "failures": c.plan.failures + 1,
                    "last_error": (c.suspect or c.error or "")[:500],
                }
            else:
                values |= {"failures": 0, "last_error": None}
            if c.complete and c.broken is None:
                values |= {
                    "job_keys": c.keys,
                    "jobs": len(c.keys),
                    "signature": plan_service.signature(c.keys),
                }
                if c.new or c.gone:
                    values["changed_at"] = finished_at
            elif c.new and c.broken is None and processed is not None and not processed.deferred:
                # Lượt chưa đọc trọn (vd. nguồn quá SOURCE_MAX_JOBS tin) không biết tin nào đã
                # gỡ, nhưng tin mới đã xử lý xong: ghi thêm vào tập mã để lượt sau không coi là
                # mới và xử lý lại. Tập mã được làm mới hẳn ở lượt đọc trọn / build đêm.
                keys = [*(c.plan.job_keys or []), *sorted(c.new)]
                values |= {"job_keys": keys, "jobs": len(keys), "changed_at": finished_at}
            await session.execute(
                update(CrawlPlan).where(CrawlPlan.id == c.plan.id).values(**values)
            )
        sched: dict[str, Any] = {
            "last_poll_at": started_at,
            "last_poll_ms": int((time.monotonic() - t0) * 1000),
            "polls": CompanySchedule.polls + 1,
            "updated_at": finished_at,
        }
        if all_ok:
            sched |= {"last_poll_ok_at": started_at, "poll_failures": 0, "last_error": None}
        else:
            sched |= {
                "poll_failures": CompanySchedule.poll_failures + 1,
                "last_error": "; ".join(
                    f"{c.plan.source}: {c.broken or c.error}" for c in checks if not c.ok
                )[:500],
            }
        if outcome.new or outcome.gone:
            sched["last_change_at"] = finished_at
        if broken and outcome.rebuild:
            sched |= {"state": ScheduleState.BROKEN.value, "next_build_at": finished_at}
        elif broken and not paused:
            # REBUILD_ON_ERROR tắt: không build lại ban ngày. Nguồn hỏng (`broken`, lượt poll bỏ
            # qua) chờ lượt build đêm; doanh nghiệp vẫn `ready`, các nguồn khác vẫn được poll.
            sched["next_build_at"] = schedule_service.next_build_at(company_id, finished_at, s)
        await session.execute(
            update(CompanySchedule).where(CompanySchedule.company_id == company_id).values(**sched)
        )
    if outcome.rebuild:
        await enqueue_builds(res, [company_id], trigger=RunTrigger.ERROR)

    outcome.fresh = all_ok
    previous_ok = schedule.last_poll_ok_at if schedule is not None else None
    if all_ok and previous_ok is not None:
        outcome.window_s = (finished_at - previous_ok).total_seconds()
    window = outcome.window_s
    log.info(
        "poll.done",
        status=outcome.status,
        new=outcome.new,
        gone=outcome.gone,
        saved=outcome.saved,
        sources=len(checks),
        fetches=len(records),
        window_s=round(window, 1) if window is not None else None,
        ms=int((time.monotonic() - t0) * 1000),
    )
    return outcome


# --- xếp job -------------------------------------------------------------------------------------


async def enqueue_builds(
    res: Resources,
    company_ids: Iterable[UUID],
    *,
    trigger: RunTrigger,
    refresh: bool | None = None,
) -> int:
    """Mỗi doanh nghiệp một job `build_company` (khoá `build:<id>`: đang chờ / chạy thì bỏ qua).
    Trả số job đã xếp."""
    s = res.settings
    n = 0
    for cid in company_ids:
        job = await res.queues.build.enqueue(
            "build_company",
            key=build_key(cid),
            timeout=s.company_timeout_seconds,
            retries=s.company_job_retries,
            company_id=str(cid),
            trigger=trigger.value,
            # None: theo lý do (build đêm / bộ khung hỏng thì tìm nguồn lại).
            **({} if refresh is None else {"refresh": refresh}),
        )
        n += job is not None
    return n


async def _enqueue_poll(res: Resources, key: str, company_id: UUID, trigger: RunTrigger) -> bool:
    job = await res.queues.poll.enqueue(
        "poll_company",
        key=key,
        timeout=res.settings.poll_timeout_seconds,
        company_id=str(company_id),
        trigger=trigger.value,
    )
    return job is not None


async def enqueue_polls(res: Resources, company_ids: Iterable[UUID], *, trigger: RunTrigger) -> int:
    """Mỗi doanh nghiệp một job `poll_company` (khoá `poll:<id>`). Trả số job đã xếp. Lý do
    `board` đặt cờ tin mới trước: job trùng một lượt poll đang chờ / chạy thì lượt đó đọc cờ."""
    ids = list(company_ids)
    if trigger is RunTrigger.BOARD:
        await mark_board_pending(res, ids)
    n = 0
    for cid in ids:
        n += await _enqueue_poll(res, poll_key(cid), cid, trigger)
    return n


# --- truy vấn cho giao diện -----------------------------------------------------------------------

# Trang danh sách / API tối đa trả ra cho mỗi nguồn (bộ khung có thể giữ vài chục trang sau).
PLAN_LIST_LIMIT = 30
PLAN_API_LIMIT = 10
# Trang chi tiết bộ khung: mã tin / danh mục mã → tên tối đa trả ra.
PLAN_JOB_LIMIT = 1000
PLAN_NAME_LIMIT = 500


async def company_crawl(
    session: AsyncSession, company_id: UUID
) -> tuple[CompanySchedule | None, list[CrawlPlan]]:
    """Lịch thu thập và bộ khung các nguồn (đang dùng trước, nguồn đã bỏ sau cùng)."""
    schedule = await session.get(CompanySchedule, company_id)
    plans = await session.scalars(
        select(CrawlPlan)
        .where(CrawlPlan.company_id == company_id)
        .order_by((CrawlPlan.status == "retired").asc(), CrawlPlan.source)
    )
    return schedule, list(plans)


def last_poll(plan: CrawlPlan) -> PlanPollResult:
    """Đường lượt poll gần nhất đã đi, suy từ cột của bộ khung (`modules/crawl/crawl_service.py:
    _poll`):
    lượt có tin mới / tin gỡ ghi `changed_at` (lúc xong, sau `polled_at` = lúc bắt đầu), lượt lỗi
    tăng `failures`, đủ POLL_MAX_FAILURES lượt thì `broken`; lượt build ghi lại `built_at`."""
    if plan.status == PlanStatus.RETIRED:
        return PlanPollResult.RETIRED
    if plan.status == PlanStatus.BROKEN:
        return PlanPollResult.BROKEN
    if plan.polled_at is None or plan.polled_at < plan.built_at:
        return PlanPollResult.NEVER
    if plan.failures:
        return PlanPollResult.RETRYING
    if plan.changed_at is not None and plan.changed_at >= plan.polled_at:
        return PlanPollResult.CHANGED
    return PlanPollResult.UNCHANGED


def _spec(data: dict[str, object] | None) -> PlanSpec | None:
    try:
        return load_spec(data)
    except ValidationError:
        return None


def _api(api: ApiSpec) -> PlanApiOut:
    pages = ceil(api.total / api.size) if api.paged and api.total and api.size else 1
    return PlanApiOut(
        url=api.url,
        method=api.method,
        embedded=api.embedded,
        paged=api.paged,
        page_param=api.key,
        by_offset=api.kind == "offset",
        size=api.size,
        total=api.total,
        pages=pages,
        listing=api.listing,
    )


def plan_view(plan: CrawlPlan) -> PlanOut:
    """Bộ khung một nguồn cho giao diện: những trang / API lượt poll đọc lại, theo thứ tự đọc."""
    loaded = _spec(plan.spec)
    spec = loaded or PlanSpec()
    listings, rendered, api_only = set(spec.listings), set(spec.rendered), set(spec.api_only)
    sitemaps = [s.url for s in spec.sitemaps]
    # Theo thứ tự lượt poll đọc: sitemap (sau API), rồi trang danh sách / danh mục.
    urls = list(dict.fromkeys([*sitemaps, *spec.listings, *spec.hubs]))
    return PlanOut(
        id=plan.id,
        source=plan.source,
        kind=plan.kind,
        url=plan.url,
        status=PlanStatus(plan.status),
        jobs=plan.jobs,
        built_at=plan.built_at,
        polled_at=plan.polled_at,
        changed_at=plan.changed_at,
        failures=plan.failures,
        last_error=plan.last_error,
        last_poll=last_poll(plan),
        readable=loaded is not None,
        ats=str((spec.ats or {}).get("kind") or "") or None,
        listings=[
            PlanPageOut(
                url=url,
                hub=url not in listings and url not in sitemaps,
                sitemap=url in sitemaps,
                rendered=url in rendered,
                api_only=url in api_only,
            )
            for url in urls[:PLAN_LIST_LIMIT]
        ],
        listings_total=len(urls),
        apis=[_api(api) for api in spec.apis[:PLAN_API_LIMIT]],
        apis_total=len(spec.apis),
        llm_listing=spec.llm_listing,
        ignored=len(spec.ignored),
        indirect=len(spec.indirect),
        pages=spec.pages,
        pages_needed=spec.demand(),
    )


@dataclass(frozen=True, slots=True)
class PostingRef:
    """Tin đã lưu ứng với một mã tin của bộ khung."""

    url: str
    status: str
    accepted: bool | None
    job_id: UUID | None
    title: str | None  # tiêu đề việc đã gộp


async def plan_detail(
    session: AsyncSession, plan_id: UUID
) -> tuple[CrawlPlan, Company, dict[str, PostingRef]] | None:
    """Bộ khung, doanh nghiệp và tin đã lưu của các mã tin trong bộ khung (theo `external_id`;
    nguồn của tin là loại nguồn của bộ khung, như lượt poll so mã)."""
    plan = await session.get(CrawlPlan, plan_id)
    if plan is None:
        return None
    company = await session.get(Company, plan.company_id)
    if company is None:
        return None
    keys = list(dict.fromkeys(plan.job_keys or []))[:PLAN_JOB_LIMIT]
    postings: dict[str, PostingRef] = {}
    if keys:
        rows = await session.execute(
            select(
                JobPosting.external_id,
                JobPosting.url,
                JobPosting.status,
                JobPosting.accepted,
                JobPosting.job_id,
                Job.title,
            )
            .outerjoin(Job, Job.id == JobPosting.job_id)
            .where(JobPosting.source == plan.kind, JobPosting.external_id.in_(keys))
        )
        for ext, url, status, accepted, job_id, title in rows:
            postings[ext] = PostingRef(url, status, accepted, job_id, title)
    return plan, company, postings


def plan_detail_view(
    plan: CrawlPlan,
    company: Company,
    postings: Mapping[str, PostingRef],
    limits: PollLimitsOut,
) -> PlanDetailOut:
    """Toàn bộ một bộ khung cho trang "Bản đồ bộ khung"."""
    spec = _spec(plan.spec) or PlanSpec()
    keys = list(dict.fromkeys(plan.job_keys or []))
    indirect = set(spec.indirect)
    jobs: list[PlanJobOut] = []
    for key in keys[:PLAN_JOB_LIMIT]:
        ref = postings.get(key)
        jobs.append(
            PlanJobOut(
                key=key,
                indirect=key in indirect,
                url=ref.url if ref else None,
                title=ref.title if ref else None,
                status=JobStatus(ref.status) if ref else None,
                accepted=ref.accepted if ref else None,
                job_id=ref.job_id if ref else None,
            )
        )
    return PlanDetailOut(
        company_id=company.id,
        company_name=company.brand_name or company.legal_name or company.input_name,
        plan=plan_view(plan),
        limits=limits,
        jobs=jobs,
        jobs_total=len(keys),
        ignored_links=list(spec.ignored),
        names=[
            PlanNameOut(kind=kind, id=item_id, name=name)
            for kind, item_id, name in spec.names[:PLAN_NAME_LIMIT]
        ],
        names_total=len(spec.names),
        spec=plan.spec or {},
    )


# --- thao tác cho API -----------------------------------------------------------------------------


async def enqueue_build(res: Resources, company_id: UUID) -> bool:
    """Build lại bộ khung một doanh nghiệp ngay (tìm nguồn lại). False nếu đang chờ / chạy."""
    return await enqueue_builds(res, [company_id], trigger=RunTrigger.MANUAL, refresh=True) > 0


async def enqueue_poll(res: Resources, company_id: UUID) -> bool:
    """Poll một doanh nghiệp ngay theo bộ khung. False nếu đang chờ / chạy."""
    return await enqueue_polls(res, [company_id], trigger=RunTrigger.MANUAL) > 0


async def crawl_status(res: Resources, *, minutes: int = 60) -> dict[str, object]:
    """Số đo độ trễ phát hiện + độ tươi + khung giờ, cho trang Tổng quan."""
    s = res.settings
    now = datetime.now(UTC)
    stats = await sla_service.poll_stats(res.redis, s.redis_prefix, minutes=minutes)
    async with res.sessionmaker() as session:
        fresh = await sla_service.freshness(session, now)
    return {
        "target_seconds": sla_service.TARGET_SECONDS,
        "poll_interval_seconds": s.poll_interval_seconds,
        "polls": stats.as_dict(),
        "freshness": fresh.as_dict(),
        "windows": windows(s, now),
    }


def windows(s: Settings, now: datetime) -> dict[str, object]:
    """Khung giờ build đêm / poll ngày (giờ CRAWL_TIMEZONE) và vị trí của `now` trong đó."""
    build_window = schedule_service.build_window(s)
    poll_window = schedule_service.poll_window(s)
    return {
        "timezone": s.crawl_timezone,
        "build": s.build_window,
        "poll": s.poll_window,
        "in_build": build_window.contains(now),
        "in_poll": poll_window.contains(now),
        "next_build_start": build_window.next_start(now),
        "next_poll_start": poll_window.next_start(now),
    }
