"""Xử lý một doanh nghiệp: KYC → tìm nguồn → thu thập → chuẩn hoá → xác minh → gộp → lưu."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import structlog
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jobcrawler.db.models import (
    Company,
    CompanyAlias,
    CompanyRun,
    FailedPage,
    SourceFetch,
    SourceProfile,
)
from jobcrawler.logging import get_logger
from jobcrawler.net.kv import RedisKV
from jobcrawler.net.urls import parse_domain_input
from jobcrawler.pipeline import dedup, kyc, normalize, store, verify
from jobcrawler.pipeline.verify import CompanyContext
from jobcrawler.resources import Resources
from jobcrawler.schemas import (
    CompanyEntity,
    CompanyIn,
    CompanyResult,
    FetchOutcome,
    JobOut,
    KycStatus,
    RawJob,
    RunStatus,
)
from jobcrawler.serp import SerpClient
from jobcrawler.services import boards as board_service
from jobcrawler.services import jobs as website_jobs
from jobcrawler.sources import boards
from jobcrawler.sources.base import FetchRecord, failed_page_rows

log = get_logger(__name__)

KYC_SOURCE = "kyc"
# Phần cuối trước timeout SAQ dành cho việc lưu (không chạy LLM nữa). Đủ rộng để DB chậm lúc tải
# cao vẫn không chạm timeout cứng (lượt chạy bị huỷ = failed).
SAVE_MARGIN_SECONDS = 8.0


@dataclass(slots=True)
class PipelineOutcome:
    status: RunStatus
    jobs_found: int = 0
    jobs: list[JobOut] = field(default_factory=list)
    incomplete_sources: list[str] = field(default_factory=list)
    error: str | None = None
    kyc_status: KycStatus | None = None
    website: str | None = None


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
    res: Resources, session: AsyncSession, company_in: CompanyIn, company_id: int | None = None
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
    serp = SerpClient.from_settings(s, res.http, RedisKV(res.redis, prefix=s.redis_prefix))
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
) -> list[JobOut]:
    """Chuẩn hoá → xác minh → gộp → lưu các tin đã thu thập; trả job đã nhận.

    Bước LLM (chấm tin nghi ngờ, phân loại trường mơ hồ) được chạy tới `deadline` cộng phần
    đệm trước timeout SAQ (chừa SAVE_MARGIN_SECONDS để lưu)."""
    s = res.settings
    now = datetime.now(UTC)
    until = deadline + max(
        0.0, s.company_timeout_seconds - s.company_deadline_seconds - SAVE_MARGIN_SECONDS
    )
    board_names = set(boards.registry())
    normalized = normalize.normalize_jobs(raws, now=now, is_board=board_names.__contains__)
    ctx = await company_context(res, company, website)
    sigs = dedup.Signatures()
    kv = RedisKV(res.redis, prefix=s.redis_prefix)  # cache câu trả lời LLM theo nội dung tin
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
    clusters = dedup.cluster_jobs(accepted, sigs=sigs)
    log.info("dedup.done", accepted=len(accepted), jobs=len(clusters))
    await normalize.classify_unclear(
        [c.canonical for c in clusters],
        res.llm,
        until=until,
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
            now=now,
            complete=complete,
        )
    return saved.jobs


async def _run_pipeline(
    res: Resources,
    state: RunState,
    company_in: CompanyIn,
    deadline: float,
    records: list[FetchRecord],
) -> PipelineOutcome:
    # Mọi bước thu thập dừng ở `deadline`; sau đó chuẩn hoá/xác minh/gộp/lưu.
    step = await run_kyc_step(res, state, company_in, deadline=deadline, records=records)
    incomplete = [KYC_SOURCE] if step.incomplete else []
    ok = RunStatus.AMBIGUOUS if step.status is KycStatus.AMBIGUOUS else RunStatus.OK
    # Website người dùng đưa (có thể là URL bảng ATS) luôn được thu; không có thì chỉ dùng domain
    # KYC khớp mạnh (đã xác minh, hoặc trang ghi đúng MST đầu vào).
    website = company_in.website or step.website
    # Profile trên các trang tuyển dụng đã map khi index; trang nào chưa có thì tìm bằng SERP
    # `site:`. KYC chưa chắc thì chỉ dùng profile khớp mạnh (cùng MST hoặc cùng website).
    await board_service.lookup_profiles(
        res,
        state.company,
        timeout=min(res.settings.board_serp_lookup_seconds, max(0.0, deadline - time.monotonic())),
        records=records,
    )
    board = await board_service.board_sources(
        res, state.company.id, strong_only=step.status is not KycStatus.VERIFIED
    )
    if not website and not board:
        log.info("company.no_website", kyc_status=step.status.value if step.status else None)
        return PipelineOutcome(
            status=RunStatus.PARTIAL if incomplete else ok,
            incomplete_sources=incomplete,
            kyc_status=step.status,
        )
    found = await website_jobs.jobs_for_website(
        res, website, deadline=deadline, records=records, extra_sources=board
    )
    incomplete += found.incomplete_sources
    # Nguồn lỗi/bị chặn (không phải hết giờ) cũng khiến lượt chạy không đủ để coi job cũ đã gỡ.
    failed = [r.name for r in found.collected.reports if r.outcome is not FetchOutcome.OK]
    jobs = await process_jobs(
        res,
        state.company,
        found.jobs,
        website=website,
        deadline=deadline,
        complete=not incomplete and not failed,
    )
    return PipelineOutcome(
        status=RunStatus.PARTIAL if incomplete else ok,
        jobs_found=len(found.jobs),
        jobs=jobs,
        incomplete_sources=incomplete,
        kyc_status=step.status,
        website=website,
    )


async def _insert_fetches(
    session: AsyncSession, run_id: UUID | None, company_id: int, records: list[FetchRecord]
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


async def _finish_run(
    res: Resources,
    run_id: UUID,
    company_id: int,
    outcome: PipelineOutcome,
    records: list[FetchRecord],
    finished_at: datetime,
    duration_ms: int,
) -> None:
    async with res.sessionmaker.begin() as session:
        await _insert_fetches(session, run_id, company_id, records)
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
                incomplete_sources=outcome.incomplete_sources,
                error=outcome.error,
            )
        )


def _display_name(company: Company) -> str:
    return company.legal_name or company.brand_name or company.input_name


async def process_company(
    res: Resources, company_in: CompanyIn, *, company_id: int | None = None
) -> CompanyResult:
    """Chạy toàn bộ pipeline cho một doanh nghiệp và ghi `company_runs`, kể cả khi lỗi.

    `company_id`: doanh nghiệp đã `import` (job từ `enqueue`) — dùng đúng bản ghi đó thay vì
    tra theo tên."""
    run_id = uuid4()
    started_at = datetime.now(UTC)
    t0 = time.monotonic()
    deadline = t0 + res.settings.company_deadline_seconds

    with structlog.contextvars.bound_contextvars(run_id=str(run_id), company=company_in.name):
        async with res.sessionmaker.begin() as session:
            resolved = await resolve(res, session, company_in, company_id)
            session.add(
                CompanyRun(
                    run_id=run_id,
                    company_id=resolved.company.id,
                    started_at=started_at,
                    status=RunStatus.RUNNING.value,
                )
            )
        state = RunState(company=resolved.company, fresh=resolved.fresh, how=resolved.how)

        with structlog.contextvars.bound_contextvars(company_id=state.company.id):
            log.info("company.start", tax_code=company_in.tax_code, website=company_in.website)
            # Giá trị khi bị huỷ (timeout SAQ, Ctrl+C): CancelledError không phải Exception.
            outcome = PipelineOutcome(status=RunStatus.FAILED, error="cancelled")
            # Ghi cả khi pipeline lỗi/bị huỷ giữa chừng.
            records: list[FetchRecord] = []
            try:
                outcome = await _run_pipeline(res, state, company_in, deadline, records)
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
    (không gắn lượt chạy)."""
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
        await _insert_fetches(session, None, state.company.id, records)
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
