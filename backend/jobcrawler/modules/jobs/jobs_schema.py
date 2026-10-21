"""Mô hình vào / ra của API việc làm và kiểm tay."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from jobcrawler.common.api import ApiOut
from jobcrawler.modules.jobs import jobs_service


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
    job: jobs_service.JobItem
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


class ReviewIn(BaseModel):
    correct: bool
    note: str | None = None


class ReviewSummaryOut(ApiOut):
    reviewed: int
    correct: int
    precision: float | None
    unreviewed_active: int
    by_source: list[SourceReviewOut]


def posting_out(p: Any) -> PostingOut:
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
