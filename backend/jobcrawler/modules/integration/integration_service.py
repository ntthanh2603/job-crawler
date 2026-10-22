"""Thao tác của API tích hợp (`integration_router.py`): doanh nghiệp theo mã bên ngoài (trạng thái,
danh sách, nạp CSV, thu thập ngay)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from pydantic import Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from jobcrawler.common.pagination import GetManyQuery, Page, ordering, paginate
from jobcrawler.common.schemas import ScheduleState
from jobcrawler.database.models import Company, CompanyRef, CompanySchedule
from jobcrawler.modules.companies import batch_service
from jobcrawler.modules.crawl import crawl_service
from jobcrawler.modules.integration import refs_service
from jobcrawler.modules.integration.integration_schema import IntegrationCompanyOut
from jobcrawler.services.resources import Resources

# Gọi thu thập ngay một doanh nghiệp tối đa một lần mỗi chừng này giây.
REFRESH_SECONDS = 60
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


class IntegrationCompanyQuery(GetManyQuery):
    sortBy: Literal["linked_at", "external_id"] = "linked_at"
    active: bool | None = Field(default=None, description="Chỉ mã đang / đã ngừng theo dõi")
    crawl_state: ScheduleState | None = Field(default=None, description="Lọc theo lịch thu thập")


def _company_rows(client_id: UUID) -> Any:
    return (
        select(CompanyRef, Company, CompanySchedule)
        .join(Company, Company.id == CompanyRef.company_id)
        .outerjoin(CompanySchedule, CompanySchedule.company_id == CompanyRef.company_id)
        .where(CompanyRef.client_id == client_id)
    )


def company_out(
    ref: CompanyRef, company: Company, schedule: CompanySchedule | None
) -> IntegrationCompanyOut:
    return IntegrationCompanyOut(
        external_id=ref.external_id,
        active=ref.active,
        company_id=company.id,
        name=company.input_name,
        legal_name=company.legal_name,
        tax_code=company.tax_code,
        website=company.input_website,
        careers_url=company.input_careers_url,
        primary_domain=company.primary_domain,
        kyc_status=company.kyc_status,
        crawl_state=schedule.state if schedule else None,
        sources=schedule.plans if schedule else 0,
        active_jobs=company.active_jobs,
        last_build_at=schedule.last_build_at if schedule else None,
        last_poll_ok_at=schedule.last_poll_ok_at if schedule else None,
        next_poll_at=schedule.next_poll_at if schedule else None,
        last_error=schedule.last_error if schedule else None,
        linked_at=ref.created_at,
    )


async def company_status(
    session: AsyncSession, client_id: UUID, external_id: str
) -> IntegrationCompanyOut | None:
    row = (
        await session.execute(_company_rows(client_id).where(CompanyRef.external_id == external_id))
    ).first()
    return None if row is None else company_out(*row)


async def list_companies(
    session: AsyncSession, client_id: UUID, query: IntegrationCompanyQuery
) -> Page[IntegrationCompanyOut]:
    stmt = _company_rows(client_id)
    if query.search.strip():
        term = f"%{query.search.strip()}%"
        stmt = stmt.where(or_(CompanyRef.external_id.ilike(term), Company.input_name.ilike(term)))
    if query.active is not None:
        stmt = stmt.where(CompanyRef.active.is_(query.active))
    if query.crawl_state is not None:
        stmt = stmt.where(CompanySchedule.state == query.crawl_state.value)
    key = CompanyRef.created_at if query.sortBy == "linked_at" else CompanyRef.external_id
    return await paginate(
        session,
        stmt,
        query.page,
        limit=query.limit,
        order_by=ordering(key, query.sortOrder, CompanyRef.external_id),
        scalars=False,
        convert=lambda row: company_out(*row),
    )


async def refresh(res: Resources, company_id: UUID, state: str | None) -> tuple[str, bool]:
    """Thu thập ngay: poll khi đã có bộ khung (`ready`), không thì build tìm nguồn lại. Mỗi doanh
    nghiệp tối đa một lần trong REFRESH_SECONDS (khoá Redis chính)."""
    kind = "poll" if state == ScheduleState.READY.value else "build"
    if state == ScheduleState.PAUSED.value:
        return kind, False
    key = f"{res.settings.redis_prefix}:integration:refresh:{company_id}"
    if not await res.redis.set(key, "1", nx=True, ex=REFRESH_SECONDS):
        return kind, False
    if kind == "poll":
        return kind, await crawl_service.enqueue_poll(res, company_id)
    return kind, await crawl_service.enqueue_build(res, company_id)


class CsvInputError(ValueError):
    """File CSV không dùng được (quá lớn, sai mã hoá, thiếu cột)."""


@dataclass(slots=True)
class CsvImport:
    results: list[refs_service.RefResult] = field(default_factory=list)
    rows: int = 0
    duplicates: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def parse_csv(content: bytes) -> batch_service.CsvReport:
    """CSV của hệ thống ngoài (cột `external_id` bắt buộc ở mọi dòng)."""
    if len(content) > MAX_UPLOAD_BYTES:
        raise CsvInputError("file quá lớn (tối đa 20 MB)")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvInputError("file không phải UTF-8") from exc
    try:
        report = batch_service.parse_companies_csv(text)
    except batch_service.CsvFormatError as exc:
        raise CsvInputError(str(exc)) from None
    missing = [r.line for r in report.rows if r.external_id is None]
    if missing and len(missing) == len(report.rows):
        raise CsvInputError("thiếu cột `external_id` (mã doanh nghiệp bên hệ thống của bạn)")
    report.errors += [f"dòng {line}: thiếu external_id" for line in missing]
    report.rows = [r for r in report.rows if r.external_id is not None]
    return report


async def import_csv(res: Resources, client_id: UUID, report: batch_service.CsvReport) -> CsvImport:
    items: Sequence[refs_service.RefItem] = [
        refs_service.RefItem(r.external_id or "", r.company, r.active) for r in report.rows
    ]
    results = await refs_service.upsert(res, client_id, items)
    return CsvImport(
        results=results,
        rows=len(report.rows),
        duplicates=report.duplicates,
        errors=report.errors,
        warnings=report.warnings,
    )
