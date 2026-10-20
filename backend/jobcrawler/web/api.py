"""API JSON (`/api/...`, tài liệu ở `/api/docs`) cho giao diện React và script ngoài.

Mỗi endpoint trả một mô hình Pydantic (`...Out`): spec `/api/openapi.json` (bản commit ở
`backend/openapi.json`, `scripts/export_openapi.py`) mô tả đúng JSON trả về, kiểu TypeScript của
giao diện sinh từ spec đó.
"""

from __future__ import annotations

import dataclasses
import math
import tempfile
import uuid
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field, WithJsonSchema
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jobcrawler.db.models import JobReview
from jobcrawler.resources import Resources
from jobcrawler.schemas import (
    API_OUTPUT,
    CompanyEntity,
    EmploymentType,
    JobLevel,
    KycStatus,
    PlanStatus,
    RunKind,
    RunTrigger,
    ScheduleState,
    WorkMode,
)
from jobcrawler.services import audit, batch, evaluate, overview, stats
from jobcrawler.web import actions, queries
from jobcrawler.web.markdown import to_html

router = APIRouter(prefix="/api", tags=["api"])

GOLDEN_DIR = Path("golden")


def res_of(request: Request) -> Resources:
    return request.app.state.res


def user_of(request: Request) -> str | None:
    return getattr(request.state, "user", None)


def actor_of(request: Request) -> str:
    """Người thao tác ghi vào nhật ký (`services/audit.py`)."""
    return audit.actor_of(user_of(request))


# --- mô hình trả về ------------------------------------------------------------------------------


class ApiOut(BaseModel):
    """Gốc của mọi mô hình trả về: trường có mặc định vẫn luôn có trong JSON, nên spec ghi là bắt
    buộc (kiểu sinh cho giao diện không thành `x?:`)."""

    model_config = API_OUTPUT


# Mốc thời gian: `datetime` (ISO 8601), hoặc chuỗi đã có sẵn trong JSON cache của trang tổng quan
# (`services/overview.py` ghi bằng `str()`) — giữ nguyên chuỗi đó. Spec chỉ ghi `date-time`.
Timestamp = Annotated[datetime | str, WithJsonSchema({"type": "string", "format": "date-time"})]


def known_values(values: Iterable[str], *, nullable: bool = False) -> WithJsonSchema:
    """Spec liệt kê `values` cho một trường chuỗi đọc từ Redis / JSON (không có ràng buộc ở DB như
    các cột enum): khi chạy không kiểm, giá trị lạ (job cũ trong queue...) vẫn trả ra nguyên văn."""
    schema: dict[str, Any] = {"type": "string", "enum": [str(v) for v in values]}
    return WithJsonSchema({"anyOf": [schema, {"type": "null"}]} if nullable else schema)


class PageOut[T](ApiOut):
    items: list[T]
    total: int  # đếm tối đa queries.COUNT_CAP
    page: int
    pages: int
    size: int = queries.PAGE_SIZE
    exact: bool = True  # False: thực tế nhiều hơn `total`


def _page_out[T](page: queries.Page[Any], items: list[T]) -> PageOut[T]:
    return PageOut(
        items=items,
        total=page.total,
        page=page.page,
        pages=page.pages,
        size=page.size,
        exact=page.exact,
    )


class CompanyItem(ApiOut):
    company: CompanyEntity
    input_website: str | None
    last_status: str | None
    last_started: datetime | None
    last_duration_ms: int | None
    last_incomplete: list[str]
    active_jobs: int


class RunOut(ApiOut):
    run_id: str
    company_id: UUID
    kind: RunKind
    trigger: RunTrigger
    # Timestamp: lượt chạy gần đây của trang tổng quan (`RecentRunOut`) đọc từ JSON cache.
    started_at: Timestamp
    finished_at: Timestamp | None
    duration_ms: int | None
    status: str
    jobs_found: int
    jobs_accepted: int
    jobs_new: int
    jobs_closed: int
    incomplete_sources: list[str]
    error: str | None


class AliasOut(ApiOut):
    alias: str
    kind: str | None


class ProfileOut(ApiOut):
    source: str
    url: str
    name_on_source: str
    website: str | None
    tax_code: str | None
    match_confidence: float | None
    match_signal: str | None
    indexed_at: datetime | None


class PostingOut(ApiOut):
    id: UUID
    source: str
    url: str
    title: str | None
    status: str
    accepted: bool | None
    confidence: float | None
    verification_reasons: list[str]
    last_seen_at: datetime
    verified_at: datetime | None


class FailedPageItem(ApiOut):
    id: UUID
    url: str
    source: str
    reason: str
    created_at: datetime


class CompanyDetailOut(ApiOut):
    company: CompanyEntity
    input_website: str | None
    note: str | None
    created_at: datetime
    kyc_evidence: Any
    aliases: list[AliasOut]
    profiles: list[ProfileOut]
    # Số đếm cho các tab; danh sách lấy theo trang ở /companies/{id}/jobs|runs|rejected.
    active_jobs: int
    inactive_jobs: int
    runs_total: int
    rejected_total: int
    failed_pages: list[FailedPageItem]


class FetchOut(ApiOut):
    source: str
    url: str
    http_status: int | None
    outcome: str
    duration_ms: int | None
    items: int | None
    error: str | None


class LogOut(ApiOut):
    ts: datetime
    level: str
    event: str
    context: dict[str, Any]


class SourceCount(ApiOut):
    source: str
    total: int
    outcomes: dict[str, int]


class RunDetailOut(ApiOut):
    run: RunOut
    company_name: str | None
    by_source: list[SourceCount]
    # Request / log lấy theo trang ở /runs/{id}/fetches|logs.
    fetches_total: int
    logs_total: int
    failed_pages: list[FailedPageItem]


class FailedPageOut(FailedPageItem):
    http_status: int | None
    run_id: str | None
    html: str


class ReviewOut(ApiOut):
    correct: bool
    note: str | None
    reviewer: str | None
    reviewed_at: datetime


class JobHtml(ApiOut):
    description: str
    requirements: str
    benefits: str


class JobDetailOut(ApiOut):
    job: queries.JobItem
    html: JobHtml
    postings: list[PostingOut]
    review: ReviewOut | None


class RecentReview(ApiOut):
    job_id: UUID
    title: str
    company_name: str
    correct: bool
    note: str | None
    reviewer: str | None
    reviewed_at: datetime


class SourceReviewOut(ApiOut):
    source: str
    reviewed: int
    correct: int
    precision: float | None


class ReviewPageOut(ApiOut):
    reviewed: int
    correct: int
    precision: float | None
    target: float
    unreviewed_active: int
    by_source: list[SourceReviewOut]
    next: JobDetailOut | None
    recent: list[RecentReview]


class EnqueueIn(BaseModel):
    company_ids: list[UUID] | None = Field(default=None, description="Các doanh nghiệp cần chạy.")
    stale_hours: float | None = Field(
        default=None, description="Chỉ doanh nghiệp chưa có lượt chạy xong trong N giờ."
    )
    all: bool = Field(default=False, description="Mọi doanh nghiệp (kết hợp stale_hours).")


class ReviewIn(BaseModel):
    correct: bool
    note: str | None = None


class AuditEventOut(ApiOut):
    id: UUID
    occurred_at: datetime
    actor: str
    action: str  # tên có chấm: `settings.update`, `company.build`... (GET /api/audit/actions)
    resource_type: str | None
    resource_id: str | None
    request_id: str | None
    # {key: {"before": ..., "after": ...}}; bí mật đã che.
    changes: dict[str, Any] | None
    metadata: dict[str, Any] | None


class AuditPageOut(ApiOut):
    items: list[AuditEventOut]
    # Id dòng cuối trang: truyền vào `before` để lấy trang sau; null khi đã hết.
    next: UUID | None


class MeOut(ApiOut):
    user: str | None
    auth: bool  # có UI_PASSWORD (hỏi đăng nhập)


class MetaOut(ApiOut):
    provinces: list[str]
    levels: list[str]
    employment_types: list[str]
    work_modes: list[str]
    sources: list[str]
    kyc_statuses: list[str]
    run_statuses: list[str]
    boards: list[str]


class QueuedOut(ApiOut):
    queued: bool  # False: đã có job cùng khoá đang chờ / chạy


class OkOut(ApiOut):
    ok: bool


class ImportOutcomeOut(ApiOut):
    # Trường của `actions.ImportOutcome` (tests/test_openapi.py kiểm khớp).
    rows: int
    created: int
    updated: int
    unchanged: int
    duplicates: int
    errors: list[str]
    warnings: list[str]
    enqueued: int | None
    already_queued: int


class EnqueueResultOut(ApiOut):
    # Trường của `batch.EnqueueResult` (tests/test_openapi.py kiểm khớp).
    selected: int
    enqueued: int
    already_queued: int


class ReviewSummaryOut(ApiOut):
    reviewed: int
    correct: int
    precision: float | None
    unreviewed_active: int
    by_source: list[SourceReviewOut]


class EvalServerOut(ApiOut):
    available: bool
    path: str


class CompanyEvalOut(ApiOut):
    # Trường của `evaluate.CompanyEval` + precision / recall (tests/test_openapi.py kiểm khớp).
    name: str
    company_id: UUID | None
    golden: int
    predicted: int
    matched: int
    run_status: str | None
    missing: list[str]
    extra: list[str]
    precision: float | None
    recall: float | None


class SourceEvalOut(ApiOut):
    # Trường của `evaluate.SourceEval` + precision / recall (tests/test_openapi.py kiểm khớp).
    source: str
    predicted: int
    matched: int
    golden: int
    precision: float | None
    recall: float | None


class EvalReportOut(ApiOut):
    precision: float | None
    recall: float | None
    golden: int
    predicted: int
    matched: int
    target_precision: float
    target_recall: float
    perf: dict[str, str]  # tên chỉ số → giá trị đã định dạng
    companies: list[CompanyEvalOut]
    sources: list[SourceEvalOut]
    warnings: list[str]
    markdown: str


# --- số liệu (`/stats`, trang tổng quan): các dataclass trong services/stats.py ------------------


class QueueStatsOut(ApiOut):
    # Trường của `stats.QueueStats` (tests/test_openapi.py kiểm khớp).
    name: str
    queued: int
    active: int
    scheduled: int
    workers: int
    complete: int
    failed: int
    retried: int
    aborted: int
    error: str | None


class RunStatsOut(ApiOut):
    # Trường của `stats.RunStats` (tests/test_openapi.py kiểm khớp).
    since: Timestamp | None
    runs: int
    companies: int
    by_status: dict[str, int]
    latest_by_status: dict[str, int]
    failed_rate: float | None
    p50_seconds: float | None
    p95_seconds: float | None
    max_seconds: float | None
    over_timeout: int
    wait_p50_seconds: float | None
    wait_p95_seconds: float | None
    jobs_found: int
    jobs_accepted: int
    companies_with_jobs: int
    first_started: Timestamp | None
    last_finished: Timestamp | None
    per_hour: float | None
    incomplete_sources: list[tuple[str, int]]
    errors: list[tuple[str, int]]


class SourceStatsOut(ApiOut):
    # Trường của `stats.SourceStats` (tests/test_openapi.py kiểm khớp).
    kind: str
    fetches: int
    outcomes: dict[str, int]
    avg_ms: int | None
    cached: int


class BoardStatsOut(ApiOut):
    # Trường của `stats.BoardStats` (tests/test_openapi.py kiểm khớp).
    source: str
    active_postings: int
    postings: int
    profiles: int
    profiles_mapped: int
    last_status: str | None
    last_started: Timestamp | None
    last_finished: Timestamp | None
    last_complete: bool | None
    last_urls_seen: int | None
    last_fetched: int | None
    last_new: int | None
    last_removed: int | None


class StorageStatsOut(ApiOut):
    # Trường của `compress.StorageStats` (tests/test_openapi.py kiểm khớp).
    kind: str
    table: str
    values: int
    stored_bytes: int
    original_bytes: int
    by_dict: dict[int, int] | None  # id từ điển zstd → số giá trị


class TargetOut(ApiOut):
    # Trường của `stats.Target` (tests/test_openapi.py kiểm khớp).
    name: str
    value: float | None
    target: str
    ok: bool | None
    kind: Literal["seconds", "rate"]


class StatsOut(ApiOut):
    # Trường của `stats.Stats` (tests/test_openapi.py kiểm khớp).
    generated_at: Timestamp
    queues: list[QueueStatsOut]
    companies: int
    kyc_status: dict[str, int]
    jobs_by_status: dict[str, int]
    jobs_by_source: list[tuple[str, int]]
    runs: RunStatsOut
    sources: list[SourceStatsOut]
    boards: list[BoardStatsOut]
    storage: list[StorageStatsOut]
    tables: list[tuple[str, int]]  # (bảng, byte trên đĩa gồm index và TOAST)
    targets: list[TargetOut]


# --- trang tổng quan (`services/overview.py`, JSON cache trong Redis) -----------------------------


class RunsHourOut(ApiOut):
    hour: Timestamp
    counts: dict[str, int]  # trạng thái → số lượt
    total: int


class FetchesHourOut(ApiOut):
    hour: Timestamp
    ok: int
    blocked: int
    error: int


class ChartTotalsOut(ApiOut):
    active_jobs: int
    companies: int
    companies_with_jobs: int
    new_jobs_7d: int
    with_salary: int


class JobsDayOut(ApiOut):
    day: date  # ngày giờ Việt Nam
    careers: int
    boards: int


class TopCompanyOut(ApiOut):
    id: UUID
    name: str
    active_jobs: int


class ChartsOut(ApiOut):
    """Phân bố trên job đang mở / doanh nghiệp (tính sẵn bằng cron); các cặp là (tên, số)."""

    generated_at: Timestamp
    totals: ChartTotalsOut
    jobs_by_day: list[JobsDayOut]
    jobs_by_source: list[tuple[str, int]]
    jobs_by_province: list[tuple[str, int]]
    jobs_by_level: list[tuple[str, int]]
    jobs_by_employment: list[tuple[str, int]]
    jobs_by_work_mode: list[tuple[str, int]]
    salary_bands: list[tuple[str, int]]
    kyc: list[tuple[str, int]]
    top_companies: list[TopCompanyOut]


class RecentRunOut(RunOut):
    company_name: str


class OverviewOut(ApiOut):
    stats: StatsOut
    chart: list[RunsHourOut]  # lượt chạy theo giờ, 24 giờ gần nhất
    fetch_chart: list[FetchesHourOut]  # request theo giờ, 24 giờ gần nhất
    charts: ChartsOut
    recent_runs: list[RecentRunOut]


# --- chuyển ORM → mô hình -----------------------------------------------------------------------


def _run(run: Any) -> RunOut:
    return RunOut(
        run_id=str(run.run_id),
        company_id=run.company_id,
        kind=run.kind,
        trigger=run.trigger,
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_ms=run.duration_ms,
        status=run.status,
        jobs_found=run.jobs_found,
        jobs_accepted=run.jobs_accepted,
        jobs_new=run.jobs_new,
        jobs_closed=run.jobs_closed,
        incomplete_sources=list(run.incomplete_sources or []),
        error=run.error,
    )


def _posting(p: Any) -> PostingOut:
    raw = p.raw if isinstance(p.raw, dict) else {}
    return PostingOut(
        id=p.id,
        source=p.source,
        url=p.url,
        title=raw.get("title"),
        status=p.status,
        accepted=p.accepted,
        confidence=p.confidence,
        verification_reasons=list(p.verification_reasons or []),
        last_seen_at=p.last_seen_at,
        verified_at=p.verified_at,
    )


def _failed_item(f: Any) -> FailedPageItem:
    return FailedPageItem(
        id=f.id, url=f.url, source=f.source, reason=f.reason, created_at=f.created_at
    )


def _source_review(s: evaluate.SourceReview) -> SourceReviewOut:
    return SourceReviewOut(
        source=s.source, reviewed=s.reviewed, correct=s.correct, precision=s.precision
    )


def _job_detail(d: queries.JobDetail) -> JobDetailOut:
    job = d.job
    review = d.review
    return JobDetailOut(
        job=job,
        html=JobHtml(
            description=to_html(job.description_md),
            requirements=to_html(job.requirements_md),
            benefits=to_html(job.benefits_md),
        ),
        postings=[_posting(p) for p in d.postings],
        review=ReviewOut(
            correct=review.correct,
            note=review.note,
            reviewer=review.reviewer,
            reviewed_at=review.reviewed_at,
        )
        if review is not None
        else None,
    )


def _job_filters(
    q: str,
    province: str,
    level: str,
    employment_type: str,
    work_mode: str,
    source: str,
    status: str,
    salary_from: float | None,
    company_id: UUID | None,
    reviewed: str,
    sort: str,
    page: int,
) -> queries.JobFilters:
    return queries.JobFilters(
        q=q,
        province=province,
        level=level,
        employment_type=employment_type,
        work_mode=work_mode,
        source=source,
        status="" if status == "all" else status,
        salary_from=salary_from,
        company_id=company_id,
        reviewed=reviewed,
        sort=sort,
        page=page,
    )


# --- chung ----------------------------------------------------------------------------------------


@router.get("/me")
async def get_me(request: Request) -> MeOut:
    settings = request.app.state.settings
    return MeOut(user=user_of(request), auth=settings.ui_password is not None)


@router.get("/meta")
async def get_meta(request: Request) -> MetaOut:
    """Giá trị cho các bộ lọc."""
    async with res_of(request).sessionmaker() as session:
        sources = await queries.job_sources(session)
    return MetaOut(
        provinces=queries.PROVINCE_NAMES,
        levels=queries.enum_values(JobLevel),
        employment_types=queries.enum_values(EmploymentType),
        work_modes=queries.enum_values(WorkMode),
        sources=sources,
        kyc_statuses=[s.value for s in KycStatus],
        run_statuses=queries.RUN_STATUSES,
        boards=actions.board_names(),
    )


@router.get("/overview")
async def get_overview(
    request: Request,
    hours: Annotated[float, Query(description="Cửa sổ giờ; 0 = tất cả")] = 24,
    storage: bool = False,
) -> OverviewOut:
    """Số liệu + biểu đồ trang tổng quan (services/overview.py: phân bố tính sẵn bằng cron,
    phần theo cửa sổ cache 60 giây)."""
    payload = await overview.build_overview(res_of(request), hours=hours or None, storage=storage)
    return OverviewOut.model_validate(payload)


@router.get("/stats")
async def get_stats(
    request: Request,
    hours: Annotated[float, Query(description="Cửa sổ giờ; 0 = tất cả")] = 24,
    storage: bool = False,
) -> StatsOut:
    st = await stats.collect_stats(res_of(request), hours=hours or None, storage=storage)
    return StatsOut.model_validate(dataclasses.asdict(st))


@router.post("/maintenance")
async def post_maintenance(request: Request) -> QueuedOut:
    res = res_of(request)
    queued = await actions.enqueue_maintenance(res)
    await audit.record_safely(
        res.sessionmaker,
        action=audit.MAINTENANCE_RUN,
        actor=actor_of(request),
        metadata={"queued": queued},
    )
    return QueuedOut(queued=queued)


# --- doanh nghiệp ---------------------------------------------------------------------------------


@router.get("/companies")
async def get_companies(
    request: Request, q: str = "", kyc: str = "", run: str = "", jobs: str = "", page: int = 1
) -> PageOut[CompanyItem]:
    f = queries.CompanyFilters(q=q, kyc=kyc, run=run, jobs=jobs, page=page)
    async with res_of(request).sessionmaker() as session:
        result = await queries.list_companies(session, f)
    return _page_out(
        result,
        [
            CompanyItem(
                company=CompanyEntity.model_validate(c),
                input_website=c.input_website,
                last_status=c.last_run_status,
                last_started=c.last_run_at,
                last_duration_ms=c.last_run_duration_ms,
                last_incomplete=list(c.last_run_incomplete or []),
                active_jobs=c.active_jobs,
            )
            for c in result.items
        ],
    )


@router.get("/companies/{company_id}")
async def get_company(request: Request, company_id: UUID) -> CompanyDetailOut:
    async with res_of(request).sessionmaker() as session:
        d = await queries.company_detail(session, company_id)
    if d is None:
        raise HTTPException(404, "Không có doanh nghiệp này")
    c = d.company
    return CompanyDetailOut(
        company=CompanyEntity.model_validate(c),
        input_website=c.input_website,
        note=c.note,
        created_at=c.created_at,
        kyc_evidence=c.kyc_evidence,
        aliases=[AliasOut(alias=a.alias, kind=a.kind) for a in d.aliases],
        profiles=[
            ProfileOut(
                source=p.source,
                url=p.url,
                name_on_source=p.name_on_source,
                website=p.website,
                tax_code=p.tax_code,
                match_confidence=p.match_confidence,
                match_signal=p.match_evidence.get("signal")
                if isinstance(p.match_evidence, dict)
                else None,
                indexed_at=p.indexed_at,
            )
            for p in d.profiles
        ],
        active_jobs=c.active_jobs,
        inactive_jobs=d.inactive_jobs,
        runs_total=d.runs,
        rejected_total=d.rejected,
        failed_pages=[_failed_item(f) for f in d.failed_pages],
    )


@router.get("/companies/{company_id}/jobs")
async def get_company_jobs(
    request: Request,
    company_id: UUID,
    status: Annotated[str, Query(description="active | inactive")] = "active",
    page: int = 1,
) -> PageOut[queries.JobItem]:
    async with res_of(request).sessionmaker() as session:
        result = await queries.company_jobs(
            session, company_id, active=status != "inactive", page=page
        )
    return _page_out(result, result.items)


@router.get("/companies/{company_id}/runs")
async def get_company_runs(request: Request, company_id: UUID, page: int = 1) -> PageOut[RunOut]:
    async with res_of(request).sessionmaker() as session:
        result = await queries.company_runs(session, company_id, page)
    return _page_out(result, [_run(r) for r in result.items])


@router.get("/companies/{company_id}/rejected")
async def get_company_rejected(
    request: Request, company_id: UUID, page: int = 1
) -> PageOut[PostingOut]:
    async with res_of(request).sessionmaker() as session:
        result = await queries.company_rejected(session, company_id, page)
    return _page_out(result, [_posting(p) for p in result.items])


@router.post("/companies/import")
async def post_import(
    request: Request,
    file: Annotated[UploadFile, File(description="companies.csv")],
    enqueue: Annotated[bool, Form()] = False,
) -> ImportOutcomeOut:
    res = res_of(request)
    try:
        outcome = await actions.import_csv(res, await file.read(), enqueue=enqueue)
    except batch.CsvFormatError as exc:
        raise HTTPException(400, str(exc)) from None
    await audit.record_safely(
        res.sessionmaker,
        action=audit.COMPANIES_IMPORT,
        actor=actor_of(request),
        metadata={
            "filename": file.filename,
            "rows": outcome.rows,
            "created": outcome.created,
            "updated": outcome.updated,
            "unchanged": outcome.unchanged,
            "duplicates": outcome.duplicates,
            "errors": len(outcome.errors),
            "enqueue": enqueue,
            "enqueued": outcome.enqueued,
            "already_queued": outcome.already_queued,
        },
    )
    return ImportOutcomeOut.model_validate(dataclasses.asdict(outcome))


@router.post("/enqueue")
async def post_enqueue(request: Request, body: EnqueueIn) -> EnqueueResultOut:
    if body.company_ids is None and body.stale_hours is None and not body.all:
        raise HTTPException(400, "Chọn company_ids, stale_hours hoặc all")
    res = res_of(request)
    result = await actions.enqueue_companies(
        res, company_ids=body.company_ids, stale_hours=body.stale_hours
    )
    ids = body.company_ids or []
    await audit.record_safely(
        res.sessionmaker,
        action=audit.COMPANIES_ENQUEUE,
        actor=actor_of(request),
        # Một doanh nghiệp: gắn vào doanh nghiệp đó để lịch sử của nó có dòng này.
        resource_type="company" if len(ids) == 1 else None,
        resource_id=ids[0] if len(ids) == 1 else None,
        metadata={
            **dataclasses.asdict(result),
            "all": body.company_ids is None,
            "stale_hours": body.stale_hours,
            "company_ids": audit.sample_ids(ids) if ids else None,
        },
    )
    return EnqueueResultOut.model_validate(dataclasses.asdict(result))


@router.get("/runs/{run_id}")
async def get_run(request: Request, run_id: uuid.UUID) -> RunDetailOut:
    async with res_of(request).sessionmaker() as session:
        d = await queries.run_detail(session, run_id)
    if d is None:
        raise HTTPException(404, "Không có lượt chạy này")
    company = d.company
    return RunDetailOut(
        run=_run(d.run),
        company_name=(company.brand_name or company.legal_name or company.input_name)
        if company
        else None,
        by_source=[SourceCount(source=s, total=n, outcomes=o) for s, n, o in d.by_source],
        fetches_total=d.fetches,
        logs_total=d.logs,
        failed_pages=[_failed_item(f) for f in d.failed_pages],
    )


@router.get("/runs/{run_id}/fetches")
async def get_run_fetches(request: Request, run_id: uuid.UUID, page: int = 1) -> PageOut[FetchOut]:
    async with res_of(request).sessionmaker() as session:
        result = await queries.run_fetches(session, run_id, page)
    return _page_out(
        result,
        [
            FetchOut(
                source=f.source,
                url=f.url,
                http_status=f.http_status,
                outcome=f.outcome,
                duration_ms=f.duration_ms,
                items=f.items,
                error=f.error,
            )
            for f in result.items
        ],
    )


@router.get("/runs/{run_id}/logs")
async def get_run_logs(request: Request, run_id: uuid.UUID, page: int = 1) -> PageOut[LogOut]:
    async with res_of(request).sessionmaker() as session:
        result = await queries.run_logs(session, run_id, page)
    return _page_out(
        result,
        [LogOut(ts=x.ts, level=x.level, event=x.event, context=x.context) for x in result.items],
    )


@router.get("/failed-pages/{page_id}")
async def get_failed_page(request: Request, page_id: UUID) -> FailedPageOut:
    async with res_of(request).sessionmaker() as session:
        page = await queries.failed_page(session, page_id)
    if page is None:
        raise HTTPException(404, "Không có trang này (đã quá hạn lưu?)")
    return FailedPageOut(
        id=page.id,
        url=page.url,
        source=page.source,
        reason=page.reason,
        created_at=page.created_at,
        http_status=page.http_status,
        run_id=str(page.run_id) if page.run_id else None,
        html=page.html or "",
    )


@router.get("/failed-pages/{page_id}/raw")
async def get_failed_page_raw(request: Request, page_id: UUID) -> PlainTextResponse:
    async with res_of(request).sessionmaker() as session:
        page = await queries.failed_page(session, page_id)
    if page is None:
        raise HTTPException(404)
    # text/plain: không để trình duyệt chạy HTML lấy từ site khác.
    return PlainTextResponse(
        page.html or "",
        headers={"Content-Disposition": f'attachment; filename="failed-page-{page_id}.html"'},
    )


# --- job ------------------------------------------------------------------------------------------


@router.get("/jobs")
async def get_jobs(
    request: Request,
    q: str = "",
    province: str = "",
    level: str = "",
    employment_type: str = "",
    work_mode: str = "",
    source: str = "",
    status: Annotated[str, Query(description="active | expired | removed | all")] = "active",
    salary_from: Annotated[float | None, Query(description="triệu VND")] = None,
    company_id: UUID | None = None,
    reviewed: Annotated[str, Query(description="no | yes | correct | wrong")] = "",
    sort: Annotated[str, Query(description="new | posted | salary | expires")] = "new",
    page: int = 1,
) -> PageOut[queries.JobItem]:
    f = _job_filters(
        q,
        province,
        level,
        employment_type,
        work_mode,
        source,
        status,
        salary_from,
        company_id,
        reviewed,
        sort,
        page,
    )
    async with res_of(request).sessionmaker() as session:
        result = await queries.list_jobs(session, f)
    return _page_out(result, result.items)


@router.get("/jobs/export")
async def get_jobs_export(
    request: Request,
    fmt: Annotated[str, Query(pattern="^(csv|jsonl)$")] = "csv",
    q: str = "",
    province: str = "",
    level: str = "",
    employment_type: str = "",
    work_mode: str = "",
    source: str = "",
    status: str = "active",
    salary_from: float | None = None,
    company_id: UUID | None = None,
    reviewed: str = "",
) -> StreamingResponse:
    f = _job_filters(
        q,
        province,
        level,
        employment_type,
        work_mode,
        source,
        status,
        salary_from,
        company_id,
        reviewed,
        "new",
        1,
    )
    media = "text/csv; charset=utf-8" if fmt == "csv" else "application/x-ndjson; charset=utf-8"
    return StreamingResponse(
        actions.export_stream(res_of(request), f, fmt),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="jobs.{fmt}"'},
    )


@router.get("/jobs/{job_id}")
async def get_job(request: Request, job_id: UUID) -> JobDetailOut:
    async with res_of(request).sessionmaker() as session:
        d = await queries.job_detail(session, job_id)
    if d is None:
        raise HTTPException(404, "Không có job này")
    return _job_detail(d)


@router.post("/jobs/{job_id}/review")
async def post_review(request: Request, job_id: UUID, body: ReviewIn) -> OkOut:
    res = res_of(request)
    if not await actions.job_exists(res, job_id):
        raise HTTPException(404, "Không có job này")
    async with res.sessionmaker.begin() as session:
        before = await _review_state(session, job_id)
        await evaluate.record_review(
            session, job_id, correct=body.correct, note=body.note, reviewer=user_of(request)
        )
        after = {"correct": body.correct, "note": (body.note or "").strip() or None}
        await audit.record(
            session,
            action=audit.JOB_REVIEW,
            actor=actor_of(request),
            resource_type="job",
            resource_id=job_id,
            changes={
                k: {"before": before.get(k), "after": v}
                for k, v in after.items()
                if before.get(k) != v
            },
            metadata={"correct": body.correct},
        )
    return OkOut(ok=True)


async def _review_state(session: AsyncSession, job_id: UUID) -> dict[str, Any]:
    """Đánh giá đang lưu của job ({} khi chưa kiểm), làm giá trị `before` của nhật ký."""
    row = (
        await session.execute(
            select(JobReview.correct, JobReview.note).where(JobReview.job_id == job_id)
        )
    ).first()
    return {"correct": row.correct, "note": row.note} if row else {}


@router.delete("/jobs/{job_id}/review")
async def delete_review(request: Request, job_id: UUID) -> OkOut:
    async with res_of(request).sessionmaker.begin() as session:
        before = await _review_state(session, job_id)
        await evaluate.delete_review(session, job_id)
        if before:
            await audit.record(
                session,
                action=audit.JOB_UNREVIEW,
                actor=actor_of(request),
                resource_type="job",
                resource_id=job_id,
                changes={k: {"before": v, "after": None} for k, v in before.items()},
            )
    return OkOut(ok=True)


# --- kiểm tay, golden set ------------------------------------------------------------------------


@router.get("/review")
async def get_review(request: Request, skip: UUID | None = None) -> ReviewPageOut:
    """Tổng hợp precision + một job đang mở chưa kiểm (ngẫu nhiên) + các đánh giá gần nhất."""
    async with res_of(request).sessionmaker() as session:
        summary = await evaluate.review_summary(session)
        next_id = await evaluate.next_unreviewed(session, exclude=skip)
        detail = await queries.job_detail(session, next_id) if next_id else None
        recent = await queries.recent_reviews(session)
    return ReviewPageOut(
        reviewed=summary.reviewed,
        correct=summary.correct,
        precision=summary.precision,
        target=evaluate.TARGET_PRECISION,
        unreviewed_active=summary.unreviewed_active,
        by_source=[_source_review(s) for s in summary.by_source],
        next=_job_detail(detail) if detail else None,
        recent=[
            RecentReview(
                job_id=r.job_id,
                title=title,
                company_name=company,
                correct=r.correct,
                note=r.note,
                reviewer=r.reviewer,
                reviewed_at=r.reviewed_at,
            )
            for r, title, company in recent
        ],
    )


@router.get("/review/summary")
async def get_review_summary(request: Request) -> ReviewSummaryOut:
    async with res_of(request).sessionmaker() as session:
        summary = await evaluate.review_summary(session)
    return ReviewSummaryOut(
        reviewed=summary.reviewed,
        correct=summary.correct,
        precision=summary.precision,
        unreviewed_active=summary.unreviewed_active,
        by_source=[_source_review(s) for s in summary.by_source],
    )


@router.get("/eval/server")
async def get_eval_server() -> EvalServerOut:
    """Có golden set trên máy chủ (thư mục golden/) không."""
    ok = (GOLDEN_DIR / "companies.csv").is_file() and (GOLDEN_DIR / "jobs.csv").is_file()
    return EvalServerOut(available=ok, path=str(GOLDEN_DIR))


@router.post("/eval")
async def post_eval(
    request: Request,
    companies: Annotated[UploadFile | None, File()] = None,
    jobs: Annotated[UploadFile | None, File()] = None,
    include_inactive: Annotated[bool, Form()] = False,
) -> EvalReportOut:
    """Precision/recall trên golden set tải lên (companies.csv + jobs.csv) hoặc thư mục golden/
    trên máy chủ."""
    res = res_of(request)
    uploaded = False
    with tempfile.TemporaryDirectory() as tmp:
        directory = GOLDEN_DIR
        if companies is not None and companies.filename and jobs is not None and jobs.filename:
            uploaded = True
            directory = Path(tmp)
            (directory / "companies.csv").write_bytes(await companies.read())
            (directory / "jobs.csv").write_bytes(await jobs.read())
        try:
            golden = evaluate.load_golden(directory)
        except (evaluate.GoldenError, batch.CsvFormatError, UnicodeDecodeError) as exc:
            raise HTTPException(400, f"Golden set không đọc được: {exc}") from None
    report = await evaluate.evaluate(res, golden, include_inactive=include_inactive)
    ids = [c.company_id for c in report.companies if c.company_id is not None]
    async with res.sessionmaker() as session:
        perf = await evaluate.latest_run_perf(session, ids)
    await audit.record_safely(
        res.sessionmaker,
        action=audit.EVAL_RUN,
        actor=actor_of(request),
        metadata={
            "golden": "upload" if uploaded else "server",
            "include_inactive": include_inactive,
            "companies": len(report.companies),
            "precision": report.precision,
            "recall": report.recall,
        },
    )
    return EvalReportOut(
        precision=report.precision,
        recall=report.recall,
        golden=report.golden,
        predicted=report.predicted,
        matched=report.matched,
        target_precision=evaluate.TARGET_PRECISION,
        target_recall=evaluate.TARGET_RECALL,
        perf=perf,
        companies=[
            CompanyEvalOut(**dataclasses.asdict(c), precision=c.precision, recall=c.recall)
            for c in report.companies
        ],
        sources=[
            SourceEvalOut(**dataclasses.asdict(s), precision=s.precision, recall=s.recall)
            for s in report.sources
        ],
        warnings=report.warnings,
        markdown=evaluate.markdown_report(report, perf=perf),
    )


# --- trang tuyển dụng, queue ---------------------------------------------------------------------


class BoardRunOut(ApiOut):
    id: UUID
    source: str
    started_at: datetime
    finished_at: datetime | None
    status: str
    complete: bool
    urls_seen: int
    fetched: int
    new: int
    updated: int
    unchanged: int
    failed: int
    removed: int
    profiles_new: int
    profiles_matched: int
    error: str | None
    # full = index đủ (đêm), tail = index đuôi (ngày, chỉ tin mới nhất).
    mode: Annotated[str, known_values(("full", "tail"))]


class BoardsOut(ApiOut):
    boards: list[BoardStatsOut]
    queue: QueueStatsOut


class BoardIndexOut(QueuedOut):
    workers: int  # runner đang nghe queue trang tuyển dụng


class QueueJobOut(ApiOut):
    key: str
    function: str
    status: str
    attempts: int
    retries: int
    timeout: int  # giây, 0 = không giới hạn
    queued: int  # ms epoch
    started: int  # ms epoch
    # Hẹn giờ chạy (giây epoch, 0 = chạy ngay); lượt tự chạy tiếp hẹn sau ~1 phút.
    scheduled: int
    error: str | None
    label: str
    company_id: str | None
    # Vì sao job được xếp (build đêm, poll theo chu kỳ, poll lỗi → build lại...).
    trigger: Annotated[str | None, known_values(RunTrigger, nullable=True)]
    # Lượt chạy tiếp thứ mấy sau lượt `partial` (0 = lượt thường).
    continuation: int


class QueueOut(QueueStatsOut):
    jobs: list[QueueJobOut]  # một trang (QUEUE_PAGE job): đang chạy → đang chờ → hẹn giờ
    page: int
    pages: int
    total: int


class QueuePageOut(ApiOut):
    queues: list[QueueOut]


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
        board_rows = await stats.board_stats(session)
    known = {b.source for b in board_rows}
    board_rows += [stats.BoardStats(source=n) for n in actions.board_names() if n not in known]
    queue = await stats.queue_stats(res_of(request).queues.boards)
    return BoardsOut.model_validate(
        {
            "boards": [dataclasses.asdict(b) for b in board_rows],
            "queue": dataclasses.asdict(queue),
        }
    )


@router.get("/boards/runs")
async def get_board_runs(
    request: Request,
    page: int = 1,
    mode: Annotated[str | None, Query(description="full | tail")] = None,
) -> PageOut[BoardRunOut]:
    async with res_of(request).sessionmaker() as session:
        result = await queries.board_runs(session, page, mode=mode)
    return _page_out(result, [_board_run(r) for r in result.items])


@router.post("/boards/{name}/index")
async def post_board_index(request: Request, name: str) -> BoardIndexOut:
    res = res_of(request)
    try:
        queued = await actions.enqueue_board(res, name)
    except KeyError:
        raise HTTPException(404, "Không có trang này") from None
    await audit.record_safely(
        res.sessionmaker,
        action=audit.BOARD_INDEX,
        actor=actor_of(request),
        resource_type="board",
        resource_id=name,
        metadata={"queued": queued},
    )
    queue = await stats.queue_stats(res.queues.boards)
    return BoardIndexOut(queued=queued, workers=queue.workers)


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


@router.get("/queue")
async def get_queue(
    request: Request, page: int = 1, poll_page: int = 1, board_page: int = 1
) -> QueuePageOut:
    """Ba queue (build, poll, trang tuyển dụng): số job, runner, và một trang các job đang
    chạy / đang chờ / hẹn giờ của mỗi queue (`page`, `poll_page`, `board_page`; mỗi trang
    QUEUE_PAGE job)."""
    out: list[QueueOut] = []
    queues = res_of(request).queues
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
        async with res_of(request).sessionmaker() as session:
            names = await queries.company_names(session, ids)
        for q in out:
            for j in q.jobs:
                if not j.label and j.company_id:
                    j.label = names.get(j.company_id, "")
    return QueuePageOut(queues=out)


# --- lịch thu thập: bộ khung, poll, độ trễ phát hiện ----------------------------------------------


class PlanOut(ApiOut):
    id: UUID
    source: str
    kind: str
    url: str
    status: PlanStatus
    jobs: int
    built_at: datetime
    polled_at: datetime | None
    changed_at: datetime | None
    failures: int
    last_error: str | None
    listings: list[str]
    hubs: int
    apis: list[str]
    rendered: int
    llm_listing: bool
    ignored: int


class ScheduleOut(ApiOut):
    state: ScheduleState
    plans: int
    next_build_at: datetime | None
    next_poll_at: datetime | None
    last_build_at: datetime | None
    last_build_status: str | None
    last_poll_at: datetime | None
    last_poll_ok_at: datetime | None
    last_poll_ms: int | None
    last_change_at: datetime | None
    poll_failures: int
    polls: int
    last_error: str | None


class CompanyCrawlOut(ApiOut):
    schedule: ScheduleOut | None
    plans: list[PlanOut]
    poll_interval_seconds: float


class PollStatsOut(ApiOut):
    """Các lượt poll trong `minutes` phút gần nhất; thời gian tính bằng giây."""

    # Trường của `sla.PollStats` (tests/test_openapi.py kiểm khớp).
    minutes: int
    polls: int
    ok: int
    failed: int
    broken: int
    skipped: int
    changed: int
    new_jobs: int
    per_second: float
    duration_p50: float | None
    duration_p95: float | None
    window_p50: float | None
    window_p95: float | None
    within_target: float | None  # tỉ lệ lượt có cửa sổ phát hiện ≤ mục tiêu (0..1)
    # Số lượt theo ngưỡng cửa sổ (giây): 30, 60, 90, 120, 135, 150, 165, 180, 210, 240, 300, 600,
    # 1800, 3600, +∞.
    window_histogram: list[int]


class FreshnessOut(ApiOut):
    """Doanh nghiệp đang poll và tuổi lượt poll đủ gần nhất (giây)."""

    # Trường của `sla.Freshness` (tests/test_openapi.py kiểm khớp).
    ready: int
    by_state: dict[ScheduleState, int]
    age_p50: float | None
    age_p95: float | None
    age_max: float | None
    overdue: int
    never_polled: int


class CrawlWindowsOut(ApiOut):
    timezone: str
    build: str  # khung giờ build, vd. "00:00-06:00" (giờ CRAWL_TIMEZONE)
    poll: str
    in_build: bool
    in_poll: bool
    next_build_start: datetime
    next_poll_start: datetime


class CrawlStatusOut(ApiOut):
    target_seconds: int  # mục tiêu cửa sổ phát hiện
    poll_interval_seconds: float
    polls: PollStatsOut
    freshness: FreshnessOut
    windows: CrawlWindowsOut


def _plan(p: Any) -> PlanOut:
    spec = p.spec or {}
    return PlanOut(
        id=p.id,
        source=p.source,
        kind=p.kind,
        url=p.url,
        status=p.status,
        jobs=p.jobs,
        built_at=p.built_at,
        polled_at=p.polled_at,
        changed_at=p.changed_at,
        failures=p.failures,
        last_error=p.last_error,
        listings=list(spec.get("listings") or [])[:20],
        hubs=len(spec.get("hubs") or []),
        apis=[a.get("url", "") for a in spec.get("apis") or [] if not a.get("embedded")][:10],
        rendered=len(spec.get("rendered") or []),
        llm_listing=bool(spec.get("llm_listing")),
        ignored=len(spec.get("ignored") or []),
    )


@router.get("/companies/{company_id}/crawl")
async def get_company_crawl(request: Request, company_id: UUID) -> CompanyCrawlOut:
    """Bộ khung các nguồn (ghi ở lượt build) và lịch build / poll của doanh nghiệp."""
    res = res_of(request)
    async with res.sessionmaker() as session:
        schedule, plans = await queries.company_crawl(session, company_id)
    return CompanyCrawlOut(
        schedule=ScheduleOut.model_validate(schedule, from_attributes=True) if schedule else None,
        plans=[_plan(p) for p in plans],
        poll_interval_seconds=res.settings.poll_interval_seconds,
    )


@router.post("/companies/{company_id}/build")
async def post_company_build(request: Request, company_id: UUID) -> QueuedOut:
    """Build lại bộ khung ngay (lượt đủ, tìm nguồn lại)."""
    res = res_of(request)
    queued = await actions.enqueue_build(res, company_id)
    await _audit_company_run(request, audit.COMPANY_BUILD, company_id, queued)
    return QueuedOut(queued=queued)


@router.post("/companies/{company_id}/poll")
async def post_company_poll(request: Request, company_id: UUID) -> QueuedOut:
    """Poll ngay theo bộ khung."""
    res = res_of(request)
    queued = await actions.enqueue_poll(res, company_id)
    await _audit_company_run(request, audit.COMPANY_POLL, company_id, queued)
    return QueuedOut(queued=queued)


async def _audit_company_run(request: Request, action: str, company_id: UUID, queued: bool) -> None:
    await audit.record_safely(
        res_of(request).sessionmaker,
        action=action,
        actor=actor_of(request),
        resource_type="company",
        resource_id=company_id,
        metadata={"queued": queued, "trigger": RunTrigger.MANUAL.value},
    )


@router.get("/crawl/status")
async def get_crawl_status(request: Request, minutes: int = 60) -> CrawlStatusOut:
    """Độ trễ phát hiện tin mới (P50/P95 cửa sổ phát hiện của các lượt poll trong `minutes` phút
    gần nhất), độ tươi hiện tại, khung giờ build / poll."""
    status = await actions.crawl_status(res_of(request), minutes=max(1, min(minutes, 24 * 60)))
    return CrawlStatusOut.model_validate(status)


# --- nhật ký thao tác -----------------------------------------------------------------------------


@router.get("/audit")
async def get_audit(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=audit.MAX_LIMIT)] = 50,
    before: Annotated[
        UUID | None, Query(description="Id dòng cuối trang trước (`next`): lấy các dòng cũ hơn.")
    ] = None,
    action: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    actor: str | None = None,
) -> AuditPageOut:
    """Nhật ký thao tác của người vận hành, mới nhất trước. `next` = id dòng cuối (truyền vào
    `before` để lấy trang sau), `null` khi đã hết."""
    async with res_of(request).sessionmaker() as session:
        events, next_id = await audit.list_events(
            session,
            limit=limit,
            before=before,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            actor=actor,
        )
    return AuditPageOut(
        items=[
            AuditEventOut(
                id=e.id,
                occurred_at=e.occurred_at,
                actor=e.actor,
                action=e.action,
                resource_type=e.resource_type,
                resource_id=e.resource_id,
                request_id=e.request_id,
                changes=e.changes,
                metadata=e.meta,
            )
            for e in events
        ],
        next=next_id,
    )


@router.get("/audit/actions")
async def get_audit_actions() -> list[str]:
    """Mọi tên thao tác được ghi nhật ký (danh sách lọc)."""
    return sorted(audit.ACTIONS)
