"""Lưu kết quả một doanh nghiệp: mọi tin vào `job_postings` (kèm quyết định xác minh), mỗi cụm đã
gộp thành một dòng `jobs`. Upsert theo khoá duy nhất nên chạy lại không sinh bản ghi trùng.

`jobs.cluster_key` ổn định qua các lượt: cụm có tin đã gắn job từ lượt trước thì dùng lại job đó
(kể cả khi bản chuẩn đổi, vd. lượt sau tìm thêm trang careers); cụm mới lấy khoá từ tin chuẩn.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from jobcrawler.db.models import Job, JobPosting
from jobcrawler.logging import get_logger
from jobcrawler.pipeline.dedup import Cluster, merge
from jobcrawler.pipeline.normalize import NormalizedJob
from jobcrawler.pipeline.verify import EXPIRED, Verdict
from jobcrawler.schemas import (
    EmploymentType,
    ExperienceYears,
    JobLevel,
    JobOut,
    JobStatus,
    Location,
    Salary,
    SalaryPeriod,
    WorkMode,
)

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _PostingRow:
    id: int
    first_seen_at: datetime
    last_seen_at: datetime
    job_id: int | None


@dataclass(slots=True)
class SaveResult:
    jobs: list[JobOut]
    postings: int
    removed: int  # job của doanh nghiệp không còn thấy (chỉ tính khi lượt chạy đủ nguồn)


def cluster_key(job: NormalizedJob) -> str:
    return hashlib.sha1(f"{job.source}:{job.external_id}".encode()).hexdigest()[:24]


def _key(job: NormalizedJob) -> tuple[str, str]:
    return (job.source, job.external_id)


async def _upsert_posting(
    session: AsyncSession, company_id: int, job: NormalizedJob, verdict: Verdict, now: datetime
) -> _PostingRow:
    expired = verdict.code == EXPIRED
    verification = {
        "company_id": company_id,
        "accepted": verdict.accepted,
        "confidence": round(verdict.confidence, 3),
        "verification_reasons": verdict.reasons,
        "verified_at": now,
    }
    content = {
        "url": job.url,
        "raw": job.raw.model_dump(mode="json"),
        "content_hash": job.content_hash,
        "last_seen_at": now,
        "expires_at": job.expires_at,
        "status": (JobStatus.EXPIRED if expired else JobStatus.ACTIVE).value,
        "missed_runs": 0,
    }
    stmt = pg_insert(JobPosting).values(
        source=job.source, external_id=job.external_id, first_seen_at=now, **content, **verification
    )
    # Tin trên trang tuyển dụng do lượt index quản lý (nội dung, last_seen_at, trạng thái): ở đây
    # chỉ ghi quyết định xác minh. Tin từ website doanh nghiệp: lượt chạy này là lần thấy mới nhất.
    set_: dict[str, Any] = dict(verification)
    if not job.board:
        set_ |= content
    elif expired:
        set_["status"] = JobStatus.EXPIRED.value
    stmt = stmt.on_conflict_do_update(
        index_elements=[JobPosting.source, JobPosting.external_id], set_=set_
    ).returning(JobPosting.id, JobPosting.first_seen_at, JobPosting.last_seen_at, JobPosting.job_id)
    row = (await session.execute(stmt)).one()
    return _PostingRow(*row)


def job_values(
    merged: NormalizedJob,
    *,
    company_id: int,
    company_name: str,
    source_urls: list[str],
    confidence: float,
    reasons: list[str],
) -> dict[str, Any]:
    salary = merged.salary
    exp = merged.experience_years
    return {
        "company_id": company_id,
        "company_name": company_name,
        "title": merged.title,
        "title_normalized": merged.title_normalized,
        "level": merged.level.value,
        "employment_type": merged.employment_type.value,
        "work_mode": merged.work_mode.value,
        "locations": [loc.model_dump(mode="json") for loc in merged.locations],
        "salary_min": salary.min if salary else None,
        "salary_max": salary.max if salary else None,
        "salary_currency": salary.currency if salary else None,
        "salary_period": salary.period.value if salary and salary.period else None,
        "salary_negotiable": bool(salary and salary.negotiable),
        "salary_raw": salary.raw if salary else None,
        "experience_years_min": exp.min if exp else None,
        "experience_years_max": exp.max if exp else None,
        "description_md": merged.description_md,
        "requirements_md": merged.requirements_md,
        "benefits_md": merged.benefits_md,
        "skills": merged.skills,
        "posted_at": merged.posted_at,
        "expires_at": merged.expires_at,
        "apply_url": merged.apply_url or merged.url,
        "source_urls": source_urls,
        "canonical_source": merged.source,
        "status": JobStatus.ACTIVE.value,
        "confidence": round(min(max(confidence, 0.0), 1.0), 3),
        "verification_reasons": reasons,
    }


def job_to_out(row: Job, *, now: datetime | None = None) -> JobOut:
    """Dòng `jobs` → `JobOut`. Có `now` thì job đã quá hạn nộp được trả về là `expired`."""
    salary = None
    if row.salary_raw or row.salary_min is not None or row.salary_max is not None:
        salary = Salary(
            min=row.salary_min,
            max=row.salary_max,
            currency=row.salary_currency,
            period=SalaryPeriod(row.salary_period) if row.salary_period else None,
            negotiable=row.salary_negotiable,
            raw=row.salary_raw,
        )
    experience = None
    if row.experience_years_min is not None or row.experience_years_max is not None:
        experience = ExperienceYears(min=row.experience_years_min, max=row.experience_years_max)
    status = JobStatus(row.status)
    if (
        now is not None
        and status is JobStatus.ACTIVE
        and row.expires_at is not None
        and row.expires_at < now
    ):
        status = JobStatus.EXPIRED
    return JobOut(
        title=row.title,
        title_normalized=row.title_normalized,
        company_id=row.company_id,
        company_name=row.company_name,
        level=JobLevel(row.level),
        employment_type=EmploymentType(row.employment_type),
        work_mode=WorkMode(row.work_mode),
        locations=[Location.model_validate(loc) for loc in row.locations or []],
        salary=salary,
        experience_years=experience,
        description_md=row.description_md,
        requirements_md=row.requirements_md,
        benefits_md=row.benefits_md,
        skills=list(row.skills or []),
        posted_at=row.posted_at,
        expires_at=row.expires_at,
        apply_url=row.apply_url,
        source_urls=list(row.source_urls or []),
        canonical_source=row.canonical_source,
        status=status,
        confidence=row.confidence,
        verification_reasons=list(row.verification_reasons or []),
        first_seen_at=row.first_seen_at,
        last_seen_at=row.last_seen_at,
    )


async def save_company_jobs(
    session: AsyncSession,
    *,
    company_id: int,
    company_name: str,
    jobs: Sequence[NormalizedJob],
    verdicts: Sequence[Verdict],
    clusters: Sequence[Cluster],
    now: datetime,
    complete: bool,
) -> SaveResult:
    """Ghi tin + job của một lượt chạy (trong transaction của `session`).

    `complete`: lượt chạy đủ mọi nguồn — job cũ không còn thấy được đánh `removed` (hoặc
    `expired` nếu đã quá hạn nộp). Lượt `partial` không đụng tới job cũ."""
    postings: dict[tuple[str, str], _PostingRow] = {}
    verdict_of: dict[tuple[str, str], Verdict] = {}
    for job, verdict in zip(jobs, verdicts, strict=True):
        postings[_key(job)] = await _upsert_posting(session, company_id, job, verdict, now)
        verdict_of[_key(job)] = verdict

    # Job đã gắn với tin của lượt trước → dùng lại khoá cụm của job đó.
    previous_ids = {
        row.job_id
        for k, row in postings.items()
        if row.job_id is not None and verdict_of[k].accepted
    }
    previous: dict[int, str] = {}
    if previous_ids:
        result = await session.execute(
            select(Job.id, Job.cluster_key).where(
                Job.id.in_(previous_ids), Job.company_id == company_id
            )
        )
        previous = {job_id: key for job_id, key in result}

    out: list[JobOut] = []
    touched: set[int] = set()
    used_keys: set[str] = set()
    for cluster in clusters:
        rows = [postings[_key(m)] for m in cluster.members]
        old = sorted({r.job_id for r in rows if r.job_id in previous})
        key = next((previous[j] for j in old if previous[j] not in used_keys), None)
        key = key or cluster_key(cluster.canonical)
        if key in used_keys:  # hai cụm của lượt này cùng tin chuẩn cũ: không xảy ra, phòng hờ
            key = cluster_key(cluster.canonical) + f"-{len(used_keys)}"
        used_keys.add(key)

        merged = merge(cluster)
        member_verdicts = [verdict_of[_key(m)] for m in cluster.members]
        reasons = list(verdict_of[_key(cluster.canonical)].reasons)
        if len(cluster.members) > 1:
            reasons.append(f"DEDUP: gộp {len(cluster.members)} tin")
        values = job_values(
            merged,
            company_id=company_id,
            company_name=company_name,
            source_urls=cluster.source_urls,
            confidence=max(v.confidence for v in member_verdicts),
            reasons=reasons,
        )
        values |= {
            "cluster_key": key,
            "canonical_posting_id": postings[_key(cluster.canonical)].id,
            "first_seen_at": min(r.first_seen_at for r in rows),
            "last_seen_at": max(r.last_seen_at for r in rows),
        }
        stmt = pg_insert(Job).values(**values)
        updates = {k: v for k, v in values.items() if k not in ("company_id", "cluster_key")}
        updates["first_seen_at"] = func.least(Job.first_seen_at, stmt.excluded.first_seen_at)
        updates["updated_at"] = func.now()
        job_row = (
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[Job.company_id, Job.cluster_key], set_=updates
                ).returning(Job)
            )
        ).scalar_one()
        touched.add(job_row.id)
        await session.execute(
            update(JobPosting)
            .where(JobPosting.id.in_([r.id for r in rows]))
            .values(job_id=job_row.id)
        )
        out.append(job_to_out(job_row))

    rejected = {k: postings[k] for k, v in verdict_of.items() if not v.accepted}
    if rejected:
        await session.execute(
            update(JobPosting)
            .where(JobPosting.id.in_([r.id for r in rejected.values()]))
            .values(job_id=None)
        )

    # Job cũ không còn tin nào được nhận: cụm đã nhập vào cụm khác, tin vừa bị loại (hết hạn...),
    # hoặc — khi lượt chạy đủ nguồn — không còn thấy tin nào.
    gone = set(previous) | {r.job_id for r in rejected.values() if r.job_id is not None}
    conditions = [Job.company_id == company_id, Job.status == JobStatus.ACTIVE.value]
    if touched:
        conditions.append(Job.id.not_in(touched))
    if not complete:
        conditions.append(Job.id.in_(gone - touched or {-1}))
    stale = (
        update(Job)
        .where(*conditions)
        .values(
            status=case(
                (Job.expires_at < now, JobStatus.EXPIRED.value), else_=JobStatus.REMOVED.value
            ),
            updated_at=func.now(),
        )
        .returning(Job.id)
    )
    removed = len((await session.execute(stale)).all())
    log.info(
        "jobs.saved",
        postings=len(postings),
        jobs=len(out),
        rejected=len(rejected),
        removed=removed,
        complete=complete,
    )
    return SaveResult(jobs=out, postings=len(postings), removed=removed)
