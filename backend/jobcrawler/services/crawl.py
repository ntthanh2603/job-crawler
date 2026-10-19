"""Build (đủ, ghi bộ khung) và poll (theo bộ khung) một doanh nghiệp; xếp job vào queue.

Build (`build_company`) là lượt `process_company` đầy đủ (services/companies.py) kèm ghi bộ khung
và lịch; lượt `partial` tự xếp lượt chạy tiếp.

Poll (`poll_company`) — ban ngày, mỗi POLL_INTERVAL_SECONDS:
1. Nạp bộ khung các nguồn website / ATS và (mã, URL) các tin đã lưu — không đọc nội dung tin.
2. Thu thập ở chế độ poll: nguồn chỉ đọc trang danh sách / API trong bộ khung, không dùng HTTP
   cache, tin đã biết được dùng lại (không mở trang chi tiết), chỉ mở trang của link mới.
3. So tập mã tin với lượt trước (`crawl_plans.job_keys`). Không đổi → chỉ cập nhật lịch: không
   ghi `company_runs`, không đụng `jobs` (đa số lượt poll đi đường này).
4. Có tin mới / tin biến mất (nguồn đọc trọn) / trang tuyển dụng báo có tin mới → chỉ tin mới
   được chuẩn hoá → xác minh → gộp (với việc đã có) → lưu; tin không còn thấy được đánh `removed`
   và việc của nó đóng nếu không còn tin nào khác. Chi phí một lượt poll theo số tin MỚI, không
   theo số tin của doanh nghiệp. Ghi `company_runs` (kind=poll).
5. Bộ khung không còn đúng (trang chính 404/410, API không còn trả tin, trang danh sách trống, lỗi
   tạm lặp lại POLL_MAX_FAILURES lần) → nguồn `broken`, doanh nghiệp được build lại ngay.

Build và poll cùng một doanh nghiệp không chạy chồng nhau (khoá Redis theo doanh nghiệp).
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
import secrets
import time
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import insert, select, tuple_, update

from jobcrawler.config import Settings
from jobcrawler.db.ids import uuid7
from jobcrawler.db.models import (
    Company,
    CompanyRun,
    CompanySchedule,
    CrawlPlan,
    FailedPage,
    JobPosting,
)
from jobcrawler.logging import get_logger
from jobcrawler.pipeline import store
from jobcrawler.pipeline.collect import CollectResult, SourceReport, collect
from jobcrawler.resources import Resources
from jobcrawler.schemas import (
    CompanyIn,
    CompanyResult,
    FetchOutcome,
    KycStatus,
    PlanStatus,
    RawJob,
    RunKind,
    RunStatus,
    RunTrigger,
    ScheduleState,
)
from jobcrawler.services import boards as board_service
from jobcrawler.services import companies, sla
from jobcrawler.services import plans as plan_service
from jobcrawler.sources.base import (
    FetchRecord,
    KnownJobs,
    Source,
    SourceLimits,
    failed_page_rows,
    is_stub,
)
from jobcrawler.sources.boards import registry as board_registry
from jobcrawler.sources.plan import PlanSpec

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
    nhanh; tin mới còn lại được mở ở lượt poll sau (lượt này không đủ nên không đóng việc nào)."""
    return dataclasses.replace(
        SourceLimits.from_settings(s),
        max_detail_pages=s.poll_max_detail_pages,
        poll_listing_pages=s.poll_max_listing_pages,
    )


def build_key(company_id: UUID) -> str:
    return f"build:{company_id}"


def poll_key(company_id: UUID) -> str:
    return f"poll:{company_id}"


# --- khoá theo doanh nghiệp --------------------------------------------------------------------

_UNLOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


@asynccontextmanager
async def company_lock(
    res: Resources, company_id: UUID, *, ttl: float, wait: float = 0
) -> AsyncIterator[bool]:
    """Khoá Redis theo doanh nghiệp (build / poll không chạy chồng). Trả True nếu giữ được khoá
    trong `wait` giây; khoá tự hết hạn sau `ttl` giây (runner chết giữa chừng)."""
    key = f"{res.settings.redis_prefix}:lock:company:{company_id}"
    token = secrets.token_hex(8)
    deadline = time.monotonic() + wait
    held = False
    while True:
        held = bool(await res.redis.set(key, token, nx=True, px=int(ttl * 1000)))
        if held or time.monotonic() >= deadline:
            break
        await asyncio.sleep(0.5)
    try:
        yield held
    finally:
        if held:
            await res.redis.eval(_UNLOCK, 1, key, token)  # pyright: ignore[reportGeneralTypeIssues]


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
    # Chờ lượt poll đang chạy (nếu có) xong; khoá hết hạn cùng lắm sau timeout của poll.
    async with company_lock(
        res, company_id, ttl=s.company_timeout_seconds + 30, wait=s.poll_timeout_seconds
    ):
        result = await companies.process_company(
            res,
            company_in,
            company_id=company_id,
            queued_at=queued_at,
            trigger=trigger,
            refresh_discovery=refresh,
        )
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
    # Cửa sổ phát hiện: lúc xong − lúc bắt đầu lượt poll đủ trước (services/sla.py).
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
    sources = await board_service.board_sources(
        res,
        company.id,
        policy=companies.profile_policy(
            None if company.kyc_status is None else KycStatus(company.kyc_status),
            name_profiles=res.settings.board_name_profiles_without_kyc,
        ),
        names=companies.company_names(company),
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
        await companies.insert_fetches(session, run.run_id, run.company_id, records)
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
    async with company_lock(res, company_id, ttl=s.poll_timeout_seconds + 15) as held:
        if not held:
            outcome.error = "doanh nghiệp đang được build"
            return outcome
        with structlog.contextvars.bound_contextvars(company_id=str(company_id), poll=True):
            try:
                outcome = await _poll(res, company_id, trigger, queued_at, started_at, t0)
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
    outcome.duration_ms = int((time.monotonic() - t0) * 1000)
    if outcome.fresh:
        metric = "ok"
    elif outcome.status in ("broken", "failed", "partial"):
        metric = "broken" if outcome.status == "broken" else "failed"
    else:
        metric = "skipped"
    await sla.record_poll(
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
    queued_at: datetime | None,
    started_at: datetime,
    t0: float,
) -> PollOutcome:
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
    if not plans and trigger is not RunTrigger.BOARD:
        outcome.status = "no_plans"
        return outcome

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
    changed = bool(outcome.new or outcome.gone) or trigger is RunTrigger.BOARD
    finished_at = datetime.now(UTC)

    run_id: UUID | None = None
    processed: companies.ProcessedJobs | None = None
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
            if trigger is RunTrigger.BOARD:
                boards = await _board_jobs(res, company, records, deadline)
                incomplete += boards.incomplete_sources
                board_jobs = boards.jobs
            processed = await companies.process_jobs(
                res,
                company,
                [*raws, *board_jobs],
                website=company.primary_domain,
                deadline=t0 + s.poll_timeout_seconds - POLL_SAVE_MARGIN,
                complete=False,
                reused=known.used,
            )
            gone = {(c.plan.kind, ext) for c in checks for ext in c.gone}
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
                incomplete.append(companies.VERIFY_SOURCE)
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
        outcome.rebuild = s.rebuild_on_error
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
        if broken:
            sched |= {"state": ScheduleState.BROKEN.value, "next_build_at": finished_at}
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


async def enqueue_polls(res: Resources, company_ids: Iterable[UUID], *, trigger: RunTrigger) -> int:
    """Mỗi doanh nghiệp một job `poll_company` (khoá `poll:<id>`). Trả số job đã xếp."""
    n = 0
    for cid in company_ids:
        job = await res.queues.poll.enqueue(
            "poll_company",
            key=poll_key(cid),
            timeout=res.settings.poll_timeout_seconds,
            company_id=str(cid),
            trigger=trigger.value,
        )
        n += job is not None
    return n
