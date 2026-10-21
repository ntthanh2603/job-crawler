"""API việc làm: `/api/jobs…` (danh sách, export, chi tiết, kiểm tay) và `/api/review…`."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jobcrawler.common.api import OkOut, PageOut, actor_of, page_out, res_of, user_of
from jobcrawler.common.markdown import to_html
from jobcrawler.database.models import JobReview
from jobcrawler.modules.audit import audit_service
from jobcrawler.modules.evaluate import evaluate_service
from jobcrawler.modules.jobs import jobs_service
from jobcrawler.modules.jobs.jobs_schema import (
    JobDetailOut,
    JobHtml,
    RecentReview,
    ReviewIn,
    ReviewOut,
    ReviewPageOut,
    ReviewSummaryOut,
    SourceReviewOut,
    posting_out,
)

router = APIRouter(prefix="/api", tags=["jobs"])


def _source_review(s: evaluate_service.SourceReview) -> SourceReviewOut:
    return SourceReviewOut(
        source=s.source, reviewed=s.reviewed, correct=s.correct, precision=s.precision
    )


def _job_detail(d: jobs_service.JobDetail) -> JobDetailOut:
    job = d.job
    review = d.review
    return JobDetailOut(
        job=job,
        html=JobHtml(
            description=to_html(job.description_md),
            requirements=to_html(job.requirements_md),
            benefits=to_html(job.benefits_md),
        ),
        postings=[posting_out(p) for p in d.postings],
        review=ReviewOut(
            correct=review.correct,
            note=review.note,
            reviewer=review.reviewer,
            reviewed_at=review.reviewed_at,
        )
        if review is not None
        else None,
    )


@router.get("/jobs")
async def get_jobs(
    request: Request, query: Annotated[jobs_service.JobListQuery, Query()]
) -> PageOut[jobs_service.JobItem]:
    """Việc làm theo bộ lọc, một trang (`search` trong tiêu đề / tên doanh nghiệp)."""
    async with res_of(request).sessionmaker() as session:
        result = await jobs_service.list_jobs(session, query)
    return page_out(result, result.items)


@router.get("/jobs/export")
async def get_jobs_export(
    request: Request, query: Annotated[jobs_service.JobExportQuery, Query()]
) -> StreamingResponse:
    """Mọi việc làm theo bộ lọc (không phân trang), CSV hoặc JSONL."""
    fmt = query.fmt
    # Tải toàn bộ dữ liệu ra ngoài: ghi nhật ký kèm bộ lọc (chỉ các bộ lọc khác mặc định).
    filters = query.model_dump(mode="json", exclude_defaults=True, exclude={"fmt"})
    await audit_service.record_safely(
        res_of(request).sessionmaker,
        action=audit_service.JOBS_EXPORT,
        actor=actor_of(request),
        resource_type="job",
        metadata={"fmt": fmt, "filters": filters or None},
    )
    media = "text/csv; charset=utf-8" if fmt == "csv" else "application/x-ndjson; charset=utf-8"
    return StreamingResponse(
        jobs_service.export_stream(res_of(request), query, fmt),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="jobs.{fmt}"'},
    )


@router.get("/jobs/{job_id}")
async def get_job(request: Request, job_id: UUID) -> JobDetailOut:
    async with res_of(request).sessionmaker() as session:
        d = await jobs_service.job_detail(session, job_id)
    if d is None:
        raise HTTPException(404, "Không có job này")
    return _job_detail(d)


@router.post("/jobs/{job_id}/review")
async def post_review(request: Request, job_id: UUID, body: ReviewIn) -> OkOut:
    res = res_of(request)
    if not await jobs_service.job_exists(res, job_id):
        raise HTTPException(404, "Không có job này")
    async with res.sessionmaker.begin() as session:
        before = await _review_state(session, job_id)
        await evaluate_service.record_review(
            session, job_id, correct=body.correct, note=body.note, reviewer=user_of(request)
        )
        after = {"correct": body.correct, "note": (body.note or "").strip() or None}
        await audit_service.record(
            session,
            action=audit_service.JOB_REVIEW,
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
        await evaluate_service.delete_review(session, job_id)
        if before:
            await audit_service.record(
                session,
                action=audit_service.JOB_UNREVIEW,
                actor=actor_of(request),
                resource_type="job",
                resource_id=job_id,
                changes={k: {"before": v, "after": None} for k, v in before.items()},
            )
    return OkOut(ok=True)


@router.get("/review")
async def get_review(request: Request, skip: UUID | None = None) -> ReviewPageOut:
    """Tổng hợp precision + một job đang mở chưa kiểm (ngẫu nhiên) + các đánh giá gần nhất."""
    async with res_of(request).sessionmaker() as session:
        summary = await evaluate_service.review_summary(session)
        next_id = await evaluate_service.next_unreviewed(session, exclude=skip)
        detail = await jobs_service.job_detail(session, next_id) if next_id else None
        recent = await jobs_service.recent_reviews(session)
    return ReviewPageOut(
        reviewed=summary.reviewed,
        correct=summary.correct,
        precision=summary.precision,
        target=evaluate_service.TARGET_PRECISION,
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
        summary = await evaluate_service.review_summary(session)
    return ReviewSummaryOut(
        reviewed=summary.reviewed,
        correct=summary.correct,
        precision=summary.precision,
        unreviewed_active=summary.unreviewed_active,
        by_source=[_source_review(s) for s in summary.by_source],
    )
