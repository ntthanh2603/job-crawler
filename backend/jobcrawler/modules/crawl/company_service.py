"""Lượt build một doanh nghiệp: KYC → tìm nguồn → thu thập → chuẩn hoá → xác minh → gộp → lưu,
rồi ghi bộ khung (`crawl_plans`) và lịch poll / build kế tiếp (`company_schedules`)."""

from __future__ import annotations

import asyncio
import dataclasses
import time
from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

import structlog
from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from jobcrawler.common.config import Settings
from jobcrawler.common.logging import get_logger
from jobcrawler.common.schemas import (
    CompanyEntity,
    CompanyIn,
    CompanyResult,
    FetchOutcome,
    JobOut,
    KycStatus,
    RawJob,
    RunKind,
    RunStatus,
    RunTrigger,
    ScheduleState,
)
from jobcrawler.common.urls import parse_domain_input
from jobcrawler.database.ids import uuid7
from jobcrawler.database.models import (
    Company,
    CompanyAlias,
    CompanyRun,
    CompanySchedule,
    FailedPage,
    SourceFetch,
    SourceProfile,
)
from jobcrawler.modules.boards import boards_service
from jobcrawler.modules.crawl import plan_service, schedule_service, website_service
from jobcrawler.pipeline import dedup, kyc, normalize, store, verify
from jobcrawler.pipeline.verify import CompanyContext
from jobcrawler.services.resources import Resources
from jobcrawler.services.serp import SerpClient
from jobcrawler.sources import boards
from jobcrawler.sources.base import FetchRecord, Source, failed_page_rows

log = get_logger(__name__)

KYC_SOURCE = "kyc"
# Có tin hoãn xác minh (LLM chưa kịp chấm) trong lượt build / poll.
VERIFY_SOURCE = "verify"
# Phần cuối trước timeout cứng của job dành cho việc lưu (không chạy LLM nữa). Đủ rộng để DB chậm
# lúc tải cao vẫn không chạm timeout cứng (lượt build bị huỷ = failed, không lưu được gì): chạy cả
# danh sách (20 build song song, 10/2026) thì lưu một doanh nghiệp mất tới ~20 giây, cộng vài giây
# chờ trước khi lượt bắt đầu. Tin LLM chưa kịp chấm được hoãn sang lượt sau nên dừng LLM sớm không
# làm mất tin.
SAVE_MARGIN_SECONDS = 30.0
# Phân loại thêm trường bằng LLM (chỉ điền trường còn `unknown`) dùng tối đa chừng này giây mỗi
# lượt: LLM quá tải thì nó chờ lâu nhất (ưu tiên thấp nhất) mà không đổi việc nhận hay loại tin.
ENRICH_SECONDS = 20.0


def build_deadline(settings: Settings, start: float) -> float:
    """Mốc dừng thu thập của lượt build: COMPANY_DEADLINE_SECONDS nhưng luôn chừa
    SAVE_MARGIN_SECONDS (không quá 1/6 timeout cứng) trước timeout cứng của job để lưu."""
    timeout = settings.company_timeout_seconds
    margin = min(SAVE_MARGIN_SECONDS, timeout / 6)
    return start + min(settings.company_deadline_seconds, timeout - margin)


@dataclass(slots=True)
class PipelineOutcome:
    status: RunStatus
    jobs_found: int = 0
    jobs: list[JobOut] = field(default_factory=list)
    incomplete_sources: list[str] = field(default_factory=list)
    error: str | None = None
    kyc_status: KycStatus | None = None
    website: str | None = None
    jobs_new: int = 0
    jobs_closed: int = 0
    plans: list[plan_service.BuiltPlan] = field(default_factory=list)
    # Tìm nguồn trên website trọn vẹn: nguồn cũ không còn thấy mới được bỏ khỏi bộ khung.
    retire_missing: bool = False
    # Nguồn careers hoá ra là trang của sàn tuyển dụng: bỏ khỏi bộ khung dù tìm nguồn chưa trọn.
    job_boards: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ProcessedJobs:
    jobs: list[JobOut]  # job đã nhận (đã gộp)
    deferred: int = 0  # tin hoãn xác minh (LLM chưa kịp chấm)
    new: int = 0  # job lần đầu thấy (tin đăng lại gắn vào job cũ không tính)
    removed: int = 0  # job bị đóng (không còn thấy ở lượt đủ, hoặc tin vừa bị loại)


@dataclass(slots=True)
class RunState:
    """Doanh nghiệp đang xử lý. KYC có thể chuyển sang bản ghi khác (trùng MST → gộp)."""

    company: Company
    fresh: bool  # KYC còn hạn, dùng luôn
    how: str | None  # cách tìm thấy trong cache: tax_code | alias | trigram


@dataclass(slots=True)
class KycStep:
    status: KycStatus | None
    website: str | None  # domain khớp mạnh để tìm nguồn
    cached: bool
    result: kyc.KycResult | None = None
    incomplete: bool = False  # hết giờ: kết quả dở, không lưu


async def resolve(
    res: Resources, session: AsyncSession, company_in: CompanyIn, company_id: UUID | None = None
) -> kyc.Resolved:
    """Doanh nghiệp theo id (job từ `enqueue`), không có thì tra cache KYC (MST, alias,
    trigram); chưa có thì tạo doanh nghiệp."""
    s = res.settings
    if company_id is not None:
        loaded = await kyc.load_company(
            session,
            company_id,
            company_in,
            max_age=timedelta(days=s.kyc_cache_days),
            serp_enabled=s.serp_enabled,
        )
        if loaded is not None:
            return loaded
        log.info("company.id_gone", company_id=company_id, reason="đã gộp/xoá, tra theo tên")
    return await kyc.resolve_company(
        session,
        company_in,
        max_age=timedelta(days=s.kyc_cache_days),
        trigram_threshold=s.kyc_trigram_threshold,
        serp_enabled=s.serp_enabled,
    )


async def run_kyc_step(
    res: Resources,
    state: RunState,
    company_in: CompanyIn,
    *,
    deadline: float,
    records: list[FetchRecord],
    refresh: bool = False,
) -> KycStep:
    """KYC còn hạn thì dùng luôn; không thì chạy KYC (tối đa KYC_TIMEOUT_SECONDS, không quá
    `deadline`) và lưu kết quả. Hết giờ giữa chừng: không lưu, chỉ dùng domain khớp mạnh."""
    company = state.company
    if state.fresh and not refresh and company.kyc_status:
        log.info(
            "kyc.cached",
            how=state.how,
            status=company.kyc_status,
            domain=company.primary_domain,
            kyc_at=company.kyc_at.isoformat() if company.kyc_at else None,
        )
        return KycStep(
            status=KycStatus(company.kyc_status), website=company.primary_domain, cached=True
        )

    s = res.settings
    serp = SerpClient.from_settings(s, res.api_http, res.cache_kv)
    result = await kyc.run_kyc(
        company_in,
        fetcher=res.http,
        llm=res.llm,
        serp=serp,
        timeout=min(s.kyc_timeout_seconds, max(1.0, deadline - time.monotonic())),
        min_confidence=s.kyc_min_confidence,
        max_sites=s.kyc_max_sites,
        records=records,
    )
    status, website = result.status, result.primary_domain
    if result.error is None:
        async with res.sessionmaker.begin() as session:
            company = await kyc.apply_kyc(session, company.id, result)
        if company.id != state.company.id:
            structlog.contextvars.bind_contextvars(company_id=company.id)
        state.company = company
        status = KycStatus(company.kyc_status) if company.kyc_status else status
        website = company.primary_domain
    log.info(
        "kyc.done",
        status=status.value,
        confidence=round(result.confidence, 2),
        domain=website,
        legal_name=result.legal_name,
        tax_code=result.tax_code,
        signals=result.signals,
        candidates=len(result.candidates),
        serp=result.serp,
        llm=result.llm,
        duration_ms=result.duration_ms,
        error=result.error,
        saved=result.error is None,
    )
    return KycStep(
        status=status,
        website=website,
        cached=False,
        result=result,
        incomplete=result.error is not None,
    )


async def company_context(res: Resources, company: Company, website: str | None) -> CompanyContext:
    """Tên, alias, profile đã map, domain của doanh nghiệp: để đối chiếu tin khi xác minh."""
    async with res.sessionmaker() as session:
        aliases = (
            await session.scalars(
                select(CompanyAlias.alias).where(CompanyAlias.company_id == company.id)
            )
        ).all()
        profiles = (
            await session.scalars(
                select(SourceProfile.name_on_source).where(SourceProfile.company_id == company.id)
            )
        ).all()
    names = [n for n in (company.legal_name, company.brand_name, company.input_name) if n]
    domains = [d for d in (company.primary_domain, *(company.other_domains or [])) if d]
    if website:
        domains.append(parse_domain_input(website))
    return CompanyContext(
        company_id=company.id,
        name=_display_name(company),
        names=list(dict.fromkeys([*names, *aliases])),
        profile_names=list(dict.fromkeys(profiles)),
        domains=domains,
        tax_code=company.tax_code,
        kyc_status=company.kyc_status,
    )


async def process_jobs(
    res: Resources,
    company: Company,
    raws: list[RawJob],
    *,
    website: str | None,
    deadline: float,
    complete: bool,
    reused: Collection[tuple[str, str]] = (),
    absent: Collection[tuple[str, str]] = (),
) -> ProcessedJobs:
    """Chuẩn hoá → xác minh → gộp → lưu các tin đã thu thập; trả job đã nhận.

    Bước LLM (chấm tin nghi ngờ, phân loại trường mơ hồ) được chạy tới `deadline` cộng phần
    đệm trước timeout cứng của job (chừa SAVE_MARGIN_SECONDS để lưu). Còn tin hoãn xác minh thì
    lượt build / poll không đủ để đóng job cũ.

    Lượt chưa đủ (`complete=False`, vd. poll chỉ có tin mới) gộp cụm của job đã lưu với các tin
    khác của job đó (`store.save_company_jobs`); `absent`: (nguồn, mã) các tin lượt này biết chắc
    đã gỡ, không gộp lại."""
    s = res.settings
    now = datetime.now(UTC)
    until = deadline + max(
        0.0, s.company_timeout_seconds - s.company_deadline_seconds - SAVE_MARGIN_SECONDS
    )
    board_names = set(boards.registry())
    # Chuẩn hoá và gom cụm là việc CPU (doanh nghiệp vài nghìn tin: hàng chục giây), chạy trong
    # thread: trên event loop thì mọi job khác của runner đứng (không tải trang, không gia hạn
    # lease) và watchdog dừng cả runner (docs/DEDUP.md mục 13).
    normalized = await asyncio.to_thread(
        normalize.normalize_jobs, raws, now=now, is_board=board_names.__contains__
    )
    ctx = await company_context(res, company, website)
    sigs = dedup.Signatures()
    kv = res.cache_kv  # cache câu trả lời LLM, chữ ký JD theo nội dung tin (Redis cache)
    # Chữ ký JD đã tính ở lượt trước: lượt đủ đêm chỉ tính lại tin đổi nội dung.
    await sigs.load(kv, (job.jd_text for job in normalized))
    verdicts = await verify.verify_jobs(
        normalized,
        ctx,
        res.llm,
        now=now,
        until=until,
        min_confidence=s.verify_min_confidence,
        min_jd_chars=s.verify_min_jd_chars,
        board_max_age=timedelta(days=s.verify_board_max_age_days),
        max_llm=s.verify_llm_max_jobs,
        sigs=sigs,
        kv=kv,
    )
    accepted = [job for job, v in zip(normalized, verdicts, strict=True) if v.accepted]
    deferred = sum(v.deferred for v in verdicts)
    clusters = await asyncio.to_thread(dedup.cluster_jobs, accepted, sigs=sigs)
    log.info("dedup.done", accepted=len(accepted), jobs=len(clusters))
    # LLM chỉ hỏi trường việc đã gộp còn `unknown`: giá trị rule của bất kỳ tin nào trong cụm đứng
    # trước đoán của LLM. Bản gộp là bản sao (cụm một tin: chính tin đó), tin giữ giá trị rule.
    merged = [dataclasses.replace(dedup.merge(c)) for c in clusters]
    await normalize.classify_unclear(
        merged,
        res.llm,
        until=min(until, time.monotonic() + ENRICH_SECONDS),
        max_jobs=s.normalize_llm_max_jobs,
        kv=kv,
    )
    async with res.sessionmaker.begin() as session:
        saved = await store.save_company_jobs(
            session,
            company_id=company.id,
            company_name=_display_name(company),
            jobs=normalized,
            verdicts=verdicts,
            clusters=clusters,
            merged=merged,
            now=now,
            complete=complete and not deferred,
            reused_postings=reused,
            is_board=board_names.__contains__,
            absent=absent,
            signatures=sigs,
            kv=kv,
        )
    return ProcessedJobs(jobs=saved.jobs, deferred=deferred, new=saved.new, removed=saved.removed)


async def _run_pipeline(
    res: Resources,
    state: RunState,
    company_in: CompanyIn,
    deadline: float,
    records: list[FetchRecord],
    *,
    refresh_discovery: bool = False,
) -> PipelineOutcome:
    # Mọi bước thu thập dừng ở `deadline`; sau đó chuẩn hoá/xác minh/gộp/lưu.
    step = await run_kyc_step(res, state, company_in, deadline=deadline, records=records)
    incomplete = [KYC_SOURCE] if step.incomplete else []
    ok = RunStatus.AMBIGUOUS if step.status is KycStatus.AMBIGUOUS else RunStatus.OK
    # Website người dùng đưa (có thể là URL bảng ATS) luôn được thu; không có thì chỉ dùng domain
    # KYC khớp mạnh (đã xác minh, hoặc trang ghi đúng MST đầu vào). Trang tuyển dụng biết sẵn
    # (`careers_url`) là điểm bắt đầu tìm nguồn; website vẫn là của doanh nghiệp (KYC, lưu tin).
    website = company_in.website or step.website
    entry = company_in.careers_url or website

    # Profile trên các trang tuyển dụng đã map khi index; trang nào chưa có thì tìm bằng SERP
    # `site:`. KYC mơ hồ thì chỉ dùng profile khớp mạnh (cùng MST hoặc cùng website); không tìm
    # thấy website thì thêm profile trùng hẳn tên (đủ riêng), vì tên là tất cả những gì có.
    async def board_list() -> list[Source]:
        await boards_service.lookup_profiles(
            res,
            state.company,
            timeout=min(
                res.settings.board_serp_lookup_seconds, max(0.0, deadline - time.monotonic())
            ),
            records=records,
        )
        return await boards_service.board_sources(
            res,
            state.company.id,
            policy=profile_policy(
                step.status, name_profiles=res.settings.board_name_profiles_without_kyc
            ),
            names=company_names(state.company),
        )

    known = await website_service.load_known_jobs(
        res,
        state.company.id,
        now=datetime.now(UTC),
        max_age_days=res.settings.job_reuse_days,
    )
    if entry:
        # Profile trên trang tuyển dụng được thu thập ngay, song song với bước tìm nguồn trên
        # website (không phải chờ discovery).
        found = await website_service.jobs_for_website(
            res,
            entry,
            deadline=deadline,
            refresh=refresh_discovery,
            records=records,
            board_sources=board_list,
            known=known,
        )
    else:
        board = await board_list()
        if not board:
            log.info("company.no_website", kyc_status=step.status.value if step.status else None)
            return PipelineOutcome(
                status=RunStatus.PARTIAL if incomplete else ok,
                incomplete_sources=incomplete,
                kyc_status=step.status,
            )
        found = await website_service.jobs_for_website(
            res, None, deadline=deadline, records=records, extra_sources=board
        )
    incomplete += found.incomplete_sources
    # Nguồn lỗi/bị chặn (không phải hết giờ) cũng khiến lượt build không đủ để coi job cũ đã gỡ.
    # Trang của sàn tuyển dụng không phải lỗi: nguồn đó không phải của doanh nghiệp.
    job_boards = [r.name for r in found.collected.reports if plan_service.job_board_source(r)]
    failed = [
        r.name
        for r in found.collected.reports
        if r.outcome is not FetchOutcome.OK and r.name not in job_boards
    ]
    processed = await process_jobs(
        res,
        state.company,
        found.jobs,
        website=website,
        deadline=deadline,
        complete=not incomplete and not failed,
        reused=known.used,
    )
    if processed.deferred:
        incomplete.append(VERIFY_SOURCE)
    discovery = found.discovery
    return PipelineOutcome(
        status=RunStatus.PARTIAL if incomplete else ok,
        jobs_found=len(found.jobs),
        jobs=processed.jobs,
        incomplete_sources=incomplete,
        kyc_status=step.status,
        website=website,
        jobs_new=processed.new,
        jobs_closed=processed.removed,
        plans=plan_service.built_plans(
            found.collected.reports,
            discovery.ats if discovery is not None else (),
            max_pages=res.settings.plan_max_pages,
            min_pages=res.settings.plan_min_pages,
        ),
        retire_missing=discovery is not None and discovery.error is None,
        job_boards=job_boards,
    )


async def insert_fetches(
    session: AsyncSession, run_id: UUID | None, company_id: UUID, records: list[FetchRecord]
) -> None:
    if not records:
        return
    await session.execute(
        insert(SourceFetch),
        [
            {
                "run_id": run_id,
                "company_id": company_id,
                "source": r.source,
                "url": r.url,
                "http_status": r.http_status,
                "proxy_id": r.proxy_id,
                "duration_ms": r.duration_ms,
                "items": r.items,
                "outcome": r.outcome.value,
                "error": r.error,
            }
            for r in records
        ],
    )


async def save_build_schedule(
    session: AsyncSession,
    res: Resources,
    company_id: UUID,
    run_id: UUID,
    outcome: PipelineOutcome,
    now: datetime,
) -> None:
    """Sau lượt build: ghi bộ khung, chuyển doanh nghiệp sang poll (có nguồn để poll) hoặc chỉ
    chờ build đêm sau (không có nguồn website / ATS), đặt mốc build đêm kế tiếp."""
    s = res.settings
    current = await session.get(CompanySchedule, company_id, with_for_update=True)
    if current is not None and current.state == ScheduleState.PAUSED.value:
        return
    values: dict[str, object] = {
        "last_build_at": now,
        "last_build_status": outcome.status.value,
        "updated_at": now,
    }
    if outcome.status is RunStatus.FAILED:
        # Lượt build lỗi: giữ bộ khung cũ; doanh nghiệp chưa có bộ khung dùng được thì thử lại sau.
        state = current.state if current is not None else ScheduleState.NEW.value
        if state in (ScheduleState.NEW.value, ScheduleState.BROKEN.value):
            values |= {
                "state": ScheduleState.BROKEN.value,
                "next_build_at": now + schedule_service.BROKEN_BACKOFF,
                "last_error": outcome.error,
            }
    else:
        usable = await plan_service.save_built_plans(
            session,
            company_id,
            outcome.plans,
            run_id=run_id,
            now=now,
            retire_missing=outcome.retire_missing,
            job_boards=outcome.job_boards,
        )
        ready = usable > 0
        values |= {
            "state": (ScheduleState.READY if ready else ScheduleState.NO_SOURCES).value,
            "plans": usable,
            "next_build_at": schedule_service.next_build_at(company_id, now, s),
            "next_poll_at": schedule_service.first_poll_at(company_id, now, s) if ready else None,
            "poll_failures": 0,
            "last_error": None,
        }
    stmt = pg_insert(CompanySchedule).values(company_id=company_id, **values)
    await session.execute(
        stmt.on_conflict_do_update(index_elements=[CompanySchedule.company_id], set_=values)
    )


async def _finish_run(
    res: Resources,
    run_id: UUID,
    company_id: UUID,
    outcome: PipelineOutcome,
    records: list[FetchRecord],
    finished_at: datetime,
    duration_ms: int,
    *,
    kind: RunKind = RunKind.BUILD,
) -> None:
    async with res.sessionmaker.begin() as session:
        await insert_fetches(session, run_id, company_id, records)
        failed = failed_page_rows(records, limit=res.settings.failed_pages_per_run, run_id=run_id)
        if failed:
            await session.execute(insert(FailedPage), failed)
        await session.execute(
            update(CompanyRun)
            .where(CompanyRun.run_id == run_id)
            .values(
                finished_at=finished_at,
                duration_ms=duration_ms,
                status=outcome.status.value,
                jobs_found=outcome.jobs_found,
                jobs_accepted=len(outcome.jobs),
                jobs_new=outcome.jobs_new,
                jobs_closed=outcome.jobs_closed,
                incomplete_sources=outcome.incomplete_sources,
                error=outcome.error,
            )
        )
        if kind is RunKind.BUILD:
            await save_build_schedule(session, res, company_id, run_id, outcome, finished_at)
        # Tóm tắt lượt mới nhất trên dòng doanh nghiệp (chỉ khi chưa có lượt nào mới hơn).
        await session.execute(
            update(Company)
            .where(Company.id == company_id, Company.last_run_id == run_id)
            .values(
                last_run_status=outcome.status.value,
                last_run_duration_ms=duration_ms,
                last_run_incomplete=outcome.incomplete_sources,
            )
        )


def profile_policy(
    status: KycStatus | None, *, name_profiles: bool
) -> boards_service.ProfilePolicy:
    if status is KycStatus.VERIFIED:
        return "all"
    if status is KycStatus.NOT_FOUND and name_profiles:
        return "exact_name"
    return "strong"


def company_names(company: Company) -> list[str]:
    return [n for n in (company.input_name, company.legal_name, company.brand_name) if n]


def _display_name(company: Company) -> str:
    return company.legal_name or company.brand_name or company.input_name


async def process_company(
    res: Resources,
    company_in: CompanyIn,
    *,
    company_id: UUID | None = None,
    queued_at: datetime | None = None,
    trigger: RunTrigger = RunTrigger.MANUAL,
    refresh_discovery: bool = False,
    deadline: float | None = None,
) -> CompanyResult:
    """Lượt build: chạy toàn bộ pipeline cho một doanh nghiệp, ghi `company_runs` (kể cả khi
    lỗi), bộ khung các nguồn và lịch poll / build kế tiếp.

    `company_id`: doanh nghiệp đã `import` — dùng đúng bản ghi đó thay vì tra theo tên.
    `queued_at`: lúc job sẵn sàng chạy (để đo thời gian chờ trong hàng đợi). `refresh_discovery`:
    tìm lại nguồn trên website, không dùng cache tìm nguồn (build đêm). `deadline`: mốc dừng thu
    thập theo `time.monotonic()` (job đã chờ khoá thì tính từ lúc job bắt đầu, để timeout cứng của
    job không cắt ngang lúc lưu); None: COMPANY_DEADLINE_SECONDS từ bây giờ."""
    run_id = uuid7()
    started_at = datetime.now(UTC)
    t0 = time.monotonic()
    if deadline is None:
        deadline = build_deadline(res.settings, t0)

    with structlog.contextvars.bound_contextvars(run_id=str(run_id), company=company_in.name):
        async with res.sessionmaker.begin() as session:
            resolved = await resolve(res, session, company_in, company_id)
            session.add(
                CompanyRun(
                    run_id=run_id,
                    company_id=resolved.company.id,
                    kind=RunKind.BUILD.value,
                    trigger=trigger.value,
                    queued_at=queued_at,
                    started_at=started_at,
                    status=RunStatus.RUNNING.value,
                )
            )
            await session.execute(
                update(Company)
                .where(Company.id == resolved.company.id)
                .values(
                    last_run_id=run_id,
                    last_run_status=RunStatus.RUNNING.value,
                    last_run_at=started_at,
                    last_run_duration_ms=None,
                    last_run_incomplete=[],
                )
            )
        state = RunState(company=resolved.company, fresh=resolved.fresh, how=resolved.how)

        with structlog.contextvars.bound_contextvars(company_id=state.company.id):
            log.info("company.start", tax_code=company_in.tax_code, website=company_in.website)
            # Giá trị khi bị huỷ (timeout job, Ctrl+C): CancelledError không phải Exception.
            outcome = PipelineOutcome(status=RunStatus.FAILED, error="cancelled")
            # Ghi cả khi pipeline lỗi/bị huỷ giữa chừng.
            records: list[FetchRecord] = []
            try:
                outcome = await _run_pipeline(
                    res, state, company_in, deadline, records, refresh_discovery=refresh_discovery
                )
            except Exception as exc:
                outcome = PipelineOutcome(
                    status=RunStatus.FAILED, error=f"{type(exc).__name__}: {exc}"
                )
                log.exception("company.error")
            finally:
                finished_at = datetime.now(UTC)
                duration_ms = int((time.monotonic() - t0) * 1000)
                await _finish_run(
                    res, run_id, state.company.id, outcome, records, finished_at, duration_ms
                )
                done = log.warning if outcome.status is RunStatus.FAILED else log.info
                done(
                    "company.done",
                    status=outcome.status.value,
                    kyc_status=outcome.kyc_status.value if outcome.kyc_status else None,
                    website=outcome.website,
                    duration_ms=duration_ms,
                    jobs_found=outcome.jobs_found,
                    jobs_accepted=len(outcome.jobs),
                    incomplete_sources=outcome.incomplete_sources,
                )

    return CompanyResult(
        run_id=run_id,
        company_id=state.company.id,
        company_name=_display_name(state.company),
        status=outcome.status,
        kyc_status=outcome.kyc_status,
        website=outcome.website,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=duration_ms,
        jobs_found=outcome.jobs_found,
        jobs_accepted=len(outcome.jobs),
        incomplete_sources=outcome.incomplete_sources,
        error=outcome.error,
        jobs=outcome.jobs,
    )


@dataclass(slots=True)
class KycReport:
    company: CompanyEntity
    aliases: list[tuple[str, str]]  # (kind, alias)
    step: KycStep
    how: str | None
    records: list[FetchRecord]


async def kyc_company(res: Resources, company_in: CompanyIn, *, refresh: bool = False) -> KycReport:
    """Chỉ chạy KYC (tra cache, tìm, lưu) cho một doanh nghiệp; fetch ghi vào `source_fetches`
    (không gắn lượt build / poll)."""
    async with res.sessionmaker.begin() as session:
        resolved = await resolve(res, session, company_in)
    state = RunState(company=resolved.company, fresh=resolved.fresh, how=resolved.how)
    records: list[FetchRecord] = []
    deadline = time.monotonic() + res.settings.kyc_timeout_seconds
    with structlog.contextvars.bound_contextvars(
        company=company_in.name, company_id=state.company.id
    ):
        step = await run_kyc_step(
            res, state, company_in, deadline=deadline, records=records, refresh=refresh
        )
    async with res.sessionmaker.begin() as session:
        await insert_fetches(session, None, state.company.id, records)
        rows = await session.execute(
            select(CompanyAlias.kind, CompanyAlias.alias)
            .where(CompanyAlias.company_id == state.company.id)
            .order_by(CompanyAlias.id)
        )
        aliases = [(kind or "", alias) for kind, alias in rows]
    return KycReport(
        company=CompanyEntity.model_validate(state.company),
        aliases=aliases,
        step=step,
        how=resolved.how,
        records=records,
    )
