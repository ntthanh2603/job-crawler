"""Xử lý một doanh nghiệp: KYC → tìm nguồn → thu thập → chuẩn hoá → xác minh → gộp → lưu."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import structlog
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jobcrawler.db.models import Company, CompanyAlias, CompanyRun, SourceFetch
from jobcrawler.logging import get_logger
from jobcrawler.resources import Resources
from jobcrawler.schemas import CompanyIn, CompanyResult, JobOut, RunStatus
from jobcrawler.services import jobs as website_jobs
from jobcrawler.sources.base import FetchRecord
from jobcrawler.text import normalize_company_name

log = get_logger(__name__)


@dataclass(slots=True)
class PipelineOutcome:
    status: RunStatus
    jobs_found: int = 0
    jobs: list[JobOut] = field(default_factory=list)
    incomplete_sources: list[str] = field(default_factory=list)
    error: str | None = None


async def ensure_company(session: AsyncSession, company_in: CompanyIn) -> Company:
    """Tìm doanh nghiệp theo MST hoặc `alias_norm` khớp chính xác; chưa có thì tạo mới."""
    alias_norm = normalize_company_name(company_in.name)
    # Khoá theo tên để hai lượt chạy song song cùng tên không tạo hai bản ghi.
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(alias_norm))))

    company: Company | None = None
    if company_in.tax_code:
        company = await session.scalar(
            select(Company).where(Company.tax_code == company_in.tax_code)
        )
    if company is None:
        company = await session.scalar(
            select(Company)
            .join(CompanyAlias, CompanyAlias.company_id == Company.id)
            .where(CompanyAlias.alias_norm == alias_norm)
            .order_by(Company.id)
            .limit(1)
        )
    if company is None:
        company = Company(input_name=company_in.name, tax_code=company_in.tax_code)
        session.add(company)
        await session.flush()
        session.add(
            CompanyAlias(
                company_id=company.id, alias=company_in.name, alias_norm=alias_norm, kind="input"
            )
        )
    return company


async def _run_pipeline(
    res: Resources,
    company: Company,
    company_in: CompanyIn,
    deadline: float,
    records: list[FetchRecord],
) -> PipelineOutcome:
    # KYC (M3) sẽ điền primary_domain; trước đó dùng website ghi trong input nếu có.
    # Chuẩn hoá/xác minh/gộp/lưu (M5) gắn sau bước thu thập. Mọi bước dừng ở `deadline`.
    website = company.primary_domain or company_in.website
    if not website:
        log.info("company.no_website", reason="chưa có website (KYC ở M3 sẽ tìm)")
        return PipelineOutcome(status=RunStatus.OK)
    found = await website_jobs.jobs_for_website(res, website, deadline=deadline, records=records)
    return PipelineOutcome(
        status=RunStatus.PARTIAL if found.incomplete_sources else RunStatus.OK,
        jobs_found=len(found.jobs),
        incomplete_sources=found.incomplete_sources,
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
        if records:
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


async def process_company(res: Resources, company_in: CompanyIn) -> CompanyResult:
    """Chạy toàn bộ pipeline cho một doanh nghiệp và ghi `company_runs`, kể cả khi lỗi."""
    run_id = uuid4()
    started_at = datetime.now(UTC)
    t0 = time.monotonic()
    deadline = t0 + res.settings.company_deadline_seconds

    with structlog.contextvars.bound_contextvars(run_id=str(run_id), company=company_in.name):
        async with res.sessionmaker.begin() as session:
            company = await ensure_company(session, company_in)
            session.add(
                CompanyRun(
                    run_id=run_id,
                    company_id=company.id,
                    started_at=started_at,
                    status=RunStatus.RUNNING.value,
                )
            )

        with structlog.contextvars.bound_contextvars(company_id=company.id):
            log.info("company.start", tax_code=company_in.tax_code, website=company_in.website)
            # Giá trị khi bị huỷ (timeout SAQ, Ctrl+C): CancelledError không phải Exception.
            outcome = PipelineOutcome(status=RunStatus.FAILED, error="cancelled")
            # Ghi cả khi pipeline lỗi/bị huỷ giữa chừng.
            records: list[FetchRecord] = []
            try:
                outcome = await _run_pipeline(res, company, company_in, deadline, records)
            except Exception as exc:
                outcome = PipelineOutcome(
                    status=RunStatus.FAILED, error=f"{type(exc).__name__}: {exc}"
                )
                log.exception("company.error")
            finally:
                finished_at = datetime.now(UTC)
                duration_ms = int((time.monotonic() - t0) * 1000)
                await _finish_run(
                    res, run_id, company.id, outcome, records, finished_at, duration_ms
                )
                done = log.warning if outcome.status is RunStatus.FAILED else log.info
                done(
                    "company.done",
                    status=outcome.status.value,
                    duration_ms=duration_ms,
                    jobs_found=outcome.jobs_found,
                    jobs_accepted=len(outcome.jobs),
                    incomplete_sources=outcome.incomplete_sources,
                )

    return CompanyResult(
        run_id=run_id,
        company_id=company.id,
        company_name=company.legal_name or company.brand_name or company.input_name,
        status=outcome.status,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=duration_ms,
        jobs_found=outcome.jobs_found,
        jobs_accepted=len(outcome.jobs),
        incomplete_sources=outcome.incomplete_sources,
        error=outcome.error,
        jobs=outcome.jobs,
    )
