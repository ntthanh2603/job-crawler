"""API JSON (`/api/...`, tài liệu ở `/api/docs`) cho giao diện React và script ngoài."""

from __future__ import annotations

import dataclasses
import math
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
from saq import Queue

from jobcrawler.resources import Resources
from jobcrawler.schemas import CompanyEntity, EmploymentType, JobLevel, KycStatus, WorkMode
from jobcrawler.services import batch, evaluate, overview, stats
from jobcrawler.web import actions, queries
from jobcrawler.web.markdown import to_html

router = APIRouter(prefix="/api", tags=["api"])

GOLDEN_DIR = Path("golden")


def res_of(request: Request) -> Resources:
    return request.app.state.res


def board_queue_of(request: Request) -> Queue:
    return request.app.state.board_queue


def user_of(request: Request) -> str | None:
    return getattr(request.state, "user", None)


# --- mô hình trả về ------------------------------------------------------------------------------


class PageOut[T](BaseModel):
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


class CompanyItem(BaseModel):
    company: CompanyEntity
    input_website: str | None
    last_status: str | None
    last_started: datetime | None
    last_duration_ms: int | None
    last_incomplete: list[str]
    active_jobs: int


class RunOut(BaseModel):
    run_id: str
    company_id: UUID
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    status: str
    jobs_found: int
    jobs_accepted: int
    incomplete_sources: list[str]
    error: str | None


class AliasOut(BaseModel):
    alias: str
    kind: str | None


class ProfileOut(BaseModel):
    source: str
    url: str
    name_on_source: str
    website: str | None
    tax_code: str | None
    match_confidence: float | None
    match_signal: str | None
    indexed_at: datetime | None


class PostingOut(BaseModel):
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


class FailedPageItem(BaseModel):
    id: UUID
    url: str
    source: str
    reason: str
    created_at: datetime


class CompanyDetailOut(BaseModel):
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


class FetchOut(BaseModel):
    source: str
    url: str
    http_status: int | None
    outcome: str
    duration_ms: int | None
    items: int | None
    error: str | None


class LogOut(BaseModel):
    ts: datetime
    level: str
    event: str
    context: dict[str, Any]


class SourceCount(BaseModel):
    source: str
    total: int
    outcomes: dict[str, int]


class RunDetailOut(BaseModel):
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


class ReviewOut(BaseModel):
    correct: bool
    note: str | None
    reviewer: str | None
    reviewed_at: datetime


class JobHtml(BaseModel):
    description: str
    requirements: str
    benefits: str


class JobDetailOut(BaseModel):
    job: queries.JobItem
    html: JobHtml
    postings: list[PostingOut]
    review: ReviewOut | None


class RecentReview(BaseModel):
    job_id: UUID
    title: str
    company_name: str
    correct: bool
    note: str | None
    reviewer: str | None
    reviewed_at: datetime


class SourceReviewOut(BaseModel):
    source: str
    reviewed: int
    correct: int
    precision: float | None


class ReviewPageOut(BaseModel):
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


# --- chuyển ORM → mô hình -----------------------------------------------------------------------


def _run(run: Any) -> RunOut:
    return RunOut(
        run_id=str(run.run_id),
        company_id=run.company_id,
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_ms=run.duration_ms,
        status=run.status,
        jobs_found=run.jobs_found,
        jobs_accepted=run.jobs_accepted,
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
async def get_me(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    return {"user": user_of(request), "auth": settings.ui_password is not None}


@router.get("/meta")
async def get_meta(request: Request) -> dict[str, Any]:
    """Giá trị cho các bộ lọc."""
    async with res_of(request).sessionmaker() as session:
        sources = await queries.job_sources(session)
    return {
        "provinces": queries.PROVINCE_NAMES,
        "levels": queries.enum_values(JobLevel),
        "employment_types": queries.enum_values(EmploymentType),
        "work_modes": queries.enum_values(WorkMode),
        "sources": sources,
        "kyc_statuses": [s.value for s in KycStatus],
        "run_statuses": queries.RUN_STATUSES,
        "boards": actions.board_names(),
    }


@router.get("/overview")
async def get_overview(
    request: Request,
    hours: Annotated[float, Query(description="Cửa sổ giờ; 0 = tất cả")] = 24,
    storage: bool = False,
) -> dict[str, Any]:
    """Số liệu + biểu đồ trang tổng quan (services/overview.py: phân bố tính sẵn bằng cron,
    phần theo cửa sổ cache 60 giây)."""
    return await overview.build_overview(res_of(request), hours=hours or None, storage=storage)


@router.get("/stats")
async def get_stats(
    request: Request,
    hours: Annotated[float, Query(description="Cửa sổ giờ; 0 = tất cả")] = 24,
    storage: bool = False,
) -> dict[str, Any]:
    st = await stats.collect_stats(res_of(request), hours=hours or None, storage=storage)
    return dataclasses.asdict(st)


@router.post("/maintenance")
async def post_maintenance(request: Request) -> dict[str, bool]:
    return {"queued": await actions.enqueue_maintenance(res_of(request))}


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
) -> dict[str, Any]:
    try:
        outcome = await actions.import_csv(res_of(request), await file.read(), enqueue=enqueue)
    except batch.CsvFormatError as exc:
        raise HTTPException(400, str(exc)) from None
    return dataclasses.asdict(outcome)


@router.post("/enqueue")
async def post_enqueue(request: Request, body: EnqueueIn) -> dict[str, int]:
    if body.company_ids is None and body.stale_hours is None and not body.all:
        raise HTTPException(400, "Chọn company_ids, stale_hours hoặc all")
    result = await actions.enqueue_companies(
        res_of(request), company_ids=body.company_ids, stale_hours=body.stale_hours
    )
    return dataclasses.asdict(result)


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
async def post_review(request: Request, job_id: UUID, body: ReviewIn) -> dict[str, bool]:
    res = res_of(request)
    if not await actions.job_exists(res, job_id):
        raise HTTPException(404, "Không có job này")
    async with res.sessionmaker.begin() as session:
        await evaluate.record_review(
            session, job_id, correct=body.correct, note=body.note, reviewer=user_of(request)
        )
    return {"ok": True}


@router.delete("/jobs/{job_id}/review")
async def delete_review(request: Request, job_id: UUID) -> dict[str, bool]:
    async with res_of(request).sessionmaker.begin() as session:
        await evaluate.delete_review(session, job_id)
    return {"ok": True}


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
        by_source=[
            SourceReviewOut(
                source=s.source, reviewed=s.reviewed, correct=s.correct, precision=s.precision
            )
            for s in summary.by_source
        ],
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
async def get_review_summary(request: Request) -> dict[str, Any]:
    async with res_of(request).sessionmaker() as session:
        summary = await evaluate.review_summary(session)
    return {
        "reviewed": summary.reviewed,
        "correct": summary.correct,
        "precision": summary.precision,
        "unreviewed_active": summary.unreviewed_active,
        "by_source": [
            {**dataclasses.asdict(s), "precision": s.precision} for s in summary.by_source
        ],
    }


@router.get("/eval/server")
async def get_eval_server() -> dict[str, Any]:
    """Có golden set trên máy chủ (thư mục golden/) không."""
    ok = (GOLDEN_DIR / "companies.csv").is_file() and (GOLDEN_DIR / "jobs.csv").is_file()
    return {"available": ok, "path": str(GOLDEN_DIR)}


@router.post("/eval")
async def post_eval(
    request: Request,
    companies: Annotated[UploadFile | None, File()] = None,
    jobs: Annotated[UploadFile | None, File()] = None,
    include_inactive: Annotated[bool, Form()] = False,
) -> dict[str, Any]:
    """Precision/recall trên golden set tải lên (companies.csv + jobs.csv) hoặc thư mục golden/
    trên máy chủ."""
    res = res_of(request)
    with tempfile.TemporaryDirectory() as tmp:
        directory = GOLDEN_DIR
        if companies is not None and companies.filename and jobs is not None and jobs.filename:
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
    return {
        "precision": report.precision,
        "recall": report.recall,
        "golden": report.golden,
        "predicted": report.predicted,
        "matched": report.matched,
        "target_precision": evaluate.TARGET_PRECISION,
        "target_recall": evaluate.TARGET_RECALL,
        "perf": perf,
        "companies": [
            {**dataclasses.asdict(c), "precision": c.precision, "recall": c.recall}
            for c in report.companies
        ],
        "sources": [
            {**dataclasses.asdict(s), "precision": s.precision, "recall": s.recall}
            for s in report.sources
        ],
        "warnings": report.warnings,
        "markdown": evaluate.markdown_report(report, perf=perf),
    }


# --- trang tuyển dụng, queue ---------------------------------------------------------------------


def _board_run(r: Any) -> dict[str, Any]:
    return {
        "id": r.id,
        "source": r.source,
        "started_at": r.started_at,
        "finished_at": r.finished_at,
        "status": r.status,
        "complete": r.complete,
        "urls_seen": r.urls_seen,
        "fetched": r.fetched,
        "new": r.new,
        "updated": r.updated,
        "unchanged": r.unchanged,
        "failed": r.failed,
        "removed": r.removed,
        "profiles_new": r.profiles_new,
        "profiles_matched": r.profiles_matched,
        "error": r.error,
    }


@router.get("/boards")
async def get_boards(request: Request) -> dict[str, Any]:
    async with res_of(request).sessionmaker() as session:
        board_rows = await stats.board_stats(session)
    known = {b.source for b in board_rows}
    board_rows += [stats.BoardStats(source=n) for n in actions.board_names() if n not in known]
    queue = await stats.queue_stats(board_queue_of(request))
    return {
        "boards": [dataclasses.asdict(b) for b in board_rows],
        "queue": dataclasses.asdict(queue),
    }


@router.get("/boards/runs")
async def get_board_runs(request: Request, page: int = 1) -> PageOut[dict[str, Any]]:
    async with res_of(request).sessionmaker() as session:
        result = await queries.board_runs(session, page)
    return _page_out(result, [_board_run(r) for r in result.items])


@router.post("/boards/{name}/index")
async def post_board_index(request: Request, name: str) -> dict[str, Any]:
    try:
        queued = await actions.enqueue_board(res_of(request), board_queue_of(request), name)
    except KeyError:
        raise HTTPException(404, "Không có trang này") from None
    queue = await stats.queue_stats(board_queue_of(request))
    return {"queued": queued, "workers": queue.workers}


def _queue_job(job: dict[str, Any]) -> dict[str, Any]:
    kwargs = job.get("kwargs") or {}
    return {
        "key": job.get("key"),
        "function": job.get("function"),
        "status": job.get("status"),
        "attempts": job.get("attempts"),
        "retries": job.get("retries"),
        "timeout": job.get("timeout"),
        "queued": job.get("queued"),
        "started": job.get("started"),
        "error": job.get("error"),
        "label": kwargs.get("name") or kwargs.get("source") or "",
        "company_id": kwargs.get("company_id"),
    }


QUEUE_PAGE = 25


@router.get("/queue")
async def get_queue(request: Request, page: int = 1, board_page: int = 1) -> dict[str, Any]:
    """Hai queue (doanh nghiệp, trang tuyển dụng): số job, worker, và một trang các job đang
    chạy / đang chờ của mỗi queue (`page`, `board_page`; mỗi trang QUEUE_PAGE job)."""
    out: list[dict[str, Any]] = []
    for queue, wanted in ((res_of(request).queue, page), (board_queue_of(request), board_page)):
        st = await stats.queue_stats(queue)
        listed = st.queued + st.active
        pages = max(1, math.ceil(listed / QUEUE_PAGE))
        current = min(max(1, wanted), pages)
        try:
            info = await queue.info(jobs=True, offset=(current - 1) * QUEUE_PAGE, limit=QUEUE_PAGE)
            jobs = [_queue_job(j) for j in info.get("jobs") or []]
        except Exception:  # Redis lỗi: vẫn trả số đếm
            jobs = []
        out.append(
            {
                **dataclasses.asdict(st),
                "jobs": jobs,
                "page": current,
                "pages": pages,
                "total": listed,
            }
        )
    return {"queues": out}
