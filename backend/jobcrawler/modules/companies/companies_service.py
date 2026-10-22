"""Truy vấn doanh nghiệp cho giao diện: danh sách có lọc, trang chi tiết.

Thao tác doanh nghiệp cho API và `jobcrawler serve`: import CSV, thêm một doanh nghiệp, đưa
vào queue, nạp danh sách mẫu khi DB trống."""

from __future__ import annotations

import codecs
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.resources import files
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, ValidationError
from sqlalchemy import Select, func, not_, or_, select, union
from sqlalchemy.ext.asyncio import AsyncSession

from jobcrawler.common.pagination import GetManyQuery, Page, capped_count, ordering, paginate
from jobcrawler.common.schemas import CompanyIn
from jobcrawler.common.text import normalize_company_name
from jobcrawler.database.models import (
    Company,
    CompanyAlias,
    CompanyRun,
    FailedPage,
    Job,
    JobPosting,
    SourceProfile,
)
from jobcrawler.modules.companies import batch_service
from jobcrawler.modules.jobs.jobs_service import JobItem, JobSort, job_item, job_ordering
from jobcrawler.modules.jobs.status_service import open_jobs
from jobcrawler.services.resources import Resources

# --- truy vấn cho giao diện -----------------------------------------------------------------------

# Cột sắp xếp danh sách doanh nghiệp → (biểu thức, có NULL không). `created_at` theo id (uuid7, PK).
COMPANY_SORTS: dict[str, tuple[Any, bool]] = {
    "created_at": (Company.id, False),
    "name": (func.coalesce(Company.brand_name, Company.legal_name, Company.input_name), False),
    "active_jobs": (Company.active_jobs, False),
    "last_run_at": (Company.last_run_at, True),
}
RUN_SORTS: dict[str, tuple[Any, bool]] = {
    "started_at": (CompanyRun.started_at, False),
    "duration_ms": (CompanyRun.duration_ms, True),
    "jobs_found": (CompanyRun.jobs_found, False),
    "jobs_new": (CompanyRun.jobs_new, False),
    "jobs_closed": (CompanyRun.jobs_closed, False),
}
REJECTED_SORTS: dict[str, tuple[Any, bool]] = {
    "verified_at": (JobPosting.verified_at, True),
    "last_seen_at": (JobPosting.last_seen_at, False),
    "confidence": (JobPosting.confidence, True),
}


class CompanyListQuery(GetManyQuery):
    """`GET /api/companies`: `search` trong tên / MST / domain / tên gọi khác."""

    kyc: str = Field(default="", description="verified | ambiguous | not_found | none")
    run: str = Field(default="", description="trạng thái lượt mới nhất | never")
    jobs: str = Field(default="", description="with | without (việc đang mở)")
    sortBy: Literal["created_at", "name", "active_jobs", "last_run_at"] = "created_at"


class CompanyJobsQuery(GetManyQuery):
    """`GET /api/companies/{id}/jobs`: `search` trong tiêu đề."""

    status: str = Field(
        default="active",
        description="active (đang mở, chưa quá hạn nộp) | inactive (hết hạn, đã gỡ)",
    )
    sortBy: JobSort = "first_seen_at"


class CompanyRunsQuery(GetManyQuery):
    """`GET /api/companies/{id}/runs`: `search` trong lỗi, hoặc đúng trạng thái / loại / lý do."""

    sortBy: Literal["started_at", "duration_ms", "jobs_found", "jobs_new", "jobs_closed"] = (
        "started_at"
    )


class CompanyRejectedQuery(GetManyQuery):
    """`GET /api/companies/{id}/rejected`: `search` trong URL tin."""

    sortBy: Literal["verified_at", "last_seen_at", "confidence"] = "verified_at"


async def list_companies(session: AsyncSession, f: CompanyListQuery) -> Page[Company]:
    """Lọc trên các cột ghi sẵn của `companies` (lượt mới nhất, số job đang mở) — không truy vấn
    con theo từng dòng; tìm kiếm qua `search_text` / `company_aliases.alias_norm` (GIN trigram)."""
    stmt = select(Company)
    if q := f.search.strip():
        # Hai nhánh, mỗi nhánh một index trigram, hợp bằng UNION (OR với EXISTS sẽ quét cả bảng).
        matches = union(
            select(Company.id).where(Company.search_text.ilike(f"%{q}%")),
            select(CompanyAlias.company_id).where(
                CompanyAlias.alias_norm.ilike(f"%{normalize_company_name(q)}%")
            ),
        )
        stmt = stmt.where(Company.id.in_(matches))
    if f.kyc == "none":
        stmt = stmt.where(Company.kyc_status.is_(None))
    elif f.kyc:
        stmt = stmt.where(Company.kyc_status == f.kyc)
    if f.run == "never":
        stmt = stmt.where(Company.last_run_status.is_(None))
    elif f.run:
        stmt = stmt.where(Company.last_run_status == f.run)
    if f.jobs == "with":
        stmt = stmt.where(Company.active_jobs > 0)
    elif f.jobs == "without":
        stmt = stmt.where(Company.active_jobs == 0)
    key, nullable = COMPANY_SORTS[f.sortBy]
    return await paginate(
        session,
        stmt,
        f.page,
        limit=f.limit,
        order_by=ordering(key, f.sortOrder, Company.id, nullable=nullable),
    )


@dataclass(slots=True)
class CompanyDetail:
    company: Company
    aliases: list[CompanyAlias]
    profiles: list[SourceProfile]
    # Số việc đang mở / không còn mở (đếm có giới hạn) theo cùng định nghĩa với `company_jobs`.
    active_jobs: int
    inactive_jobs: int
    runs: int  # số lượt build / poll (đếm có giới hạn)
    rejected: int  # số tin bị loại (đếm có giới hạn)
    failed_pages: list[FailedPage]


async def company_names(session: AsyncSession, ids: set[str]) -> dict[str, str]:
    """Tên hiển thị theo id doanh nghiệp (chuỗi); id không hợp lệ bị bỏ qua."""
    valid: list[UUID] = []
    for value in ids:
        try:
            valid.append(UUID(value))
        except ValueError:
            continue
    if not valid:
        return {}
    rows = await session.execute(
        select(
            Company.id, func.coalesce(Company.legal_name, Company.brand_name, Company.input_name)
        ).where(Company.id.in_(valid))
    )
    return {str(cid): name for cid, name in rows}


async def company_detail(session: AsyncSession, company_id: UUID) -> CompanyDetail | None:
    """Thông tin doanh nghiệp + số đếm cho các tab; danh sách việc làm, lượt build / poll, tin bị
    loại lấy theo trang (`company_jobs`, `company_runs`, `company_rejected`)."""
    company = await session.get(Company, company_id)
    if company is None:
        return None
    aliases = list(
        await session.scalars(
            select(CompanyAlias)
            .where(CompanyAlias.company_id == company_id)
            .order_by(CompanyAlias.id)
            .limit(200)
        )
    )
    profiles = list(
        await session.scalars(
            select(SourceProfile)
            .where(SourceProfile.company_id == company_id)
            .order_by(SourceProfile.source)
            .limit(100)
        )
    )
    now = datetime.now(UTC)
    jobs = select(Job.id).where(Job.company_id == company_id)
    active, _ = await capped_count(session, jobs.where(open_jobs(now)))
    inactive, _ = await capped_count(session, jobs.where(not_(open_jobs(now))))
    runs, _ = await capped_count(
        session, select(CompanyRun.run_id).where(CompanyRun.company_id == company_id)
    )
    rejected, _ = await capped_count(session, _rejected_query(company_id))
    recent = select(CompanyRun.run_id).where(CompanyRun.company_id == company_id)
    failed = list(
        await session.scalars(
            select(FailedPage)
            .where(FailedPage.run_id.in_(recent.order_by(CompanyRun.started_at.desc()).limit(30)))
            .order_by(FailedPage.created_at.desc())
            .limit(50)
        )
    )
    return CompanyDetail(
        company=company,
        aliases=aliases,
        profiles=profiles,
        active_jobs=active,
        inactive_jobs=inactive,
        runs=runs,
        rejected=rejected,
        failed_pages=failed,
    )


async def company_jobs(
    session: AsyncSession, company_id: UUID, f: CompanyJobsQuery | None = None
) -> Page[JobItem]:
    """Việc làm của một doanh nghiệp (index company_id, status, first_seen_at). Đang mở như
    `status=active` của danh sách việc làm (`status_service.open_jobs`)."""
    f = f or CompanyJobsQuery()
    now = datetime.now(UTC)
    status = not_(open_jobs(now)) if f.status == "inactive" else open_jobs(now)
    stmt = select(Job).where(Job.company_id == company_id, status)
    if search := f.search.strip():
        stmt = stmt.where(Job.title.ilike(f"%{search}%"))
    return await paginate(
        session,
        stmt,
        f.page,
        limit=f.limit,
        order_by=job_ordering(f.sortBy, f.sortOrder),
        convert=lambda row: job_item(row, now=now),
    )


async def company_runs(
    session: AsyncSession, company_id: UUID, f: CompanyRunsQuery | None = None
) -> Page[CompanyRun]:
    f = f or CompanyRunsQuery()
    stmt = select(CompanyRun).where(CompanyRun.company_id == company_id)
    if search := f.search.strip():
        stmt = stmt.where(
            or_(
                CompanyRun.error.ilike(f"%{search}%"),
                CompanyRun.status == search,
                CompanyRun.kind == search,
                CompanyRun.trigger == search,
            )
        )
    key, nullable = RUN_SORTS[f.sortBy]
    return await paginate(
        session,
        stmt,
        f.page,
        limit=f.limit,
        order_by=ordering(key, f.sortOrder, CompanyRun.run_id, nullable=nullable),
    )


def _rejected_query(company_id: UUID) -> Select[JobPosting]:
    return select(JobPosting).where(
        JobPosting.company_id == company_id, JobPosting.accepted.is_(False)
    )


async def company_rejected(
    session: AsyncSession, company_id: UUID, f: CompanyRejectedQuery | None = None
) -> Page[JobPosting]:
    """Tin bị loại (index ix_job_postings_company_rejected)."""
    f = f or CompanyRejectedQuery()
    stmt = _rejected_query(company_id)
    if search := f.search.strip():
        stmt = stmt.where(JobPosting.url.ilike(f"%{search}%"))
    key, nullable = REJECTED_SORTS[f.sortBy]
    return await paginate(
        session,
        stmt,
        f.page,
        limit=f.limit,
        order_by=ordering(key, f.sortOrder, JobPosting.id, nullable=nullable),
    )


# --- thao tác cho API -----------------------------------------------------------------------------

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
# 50 doanh nghiệp lớn của Việt Nam (tên + website) để thử hệ thống: nạp khi DB trống
# (`seed_demo_if_empty`), tải về làm file mẫu ở trang Doanh nghiệp (`demo_csv`).
DEMO_COMPANIES_CSV = files("jobcrawler").joinpath("data", "demo_companies.csv")


async def enqueue_companies(
    res: Resources,
    *,
    company_ids: Sequence[UUID] | None = None,
    stale_hours: float | None = None,
) -> batch_service.EnqueueResult:
    """Build bấm từ giao diện (nút Build, thêm / nạp doanh nghiệp). `company_ids` None = mọi
    doanh nghiệp (lọc `stale_hours` nếu có). Tìm nguồn lại và đọc trang mới: người dùng bấm
    Build là để thấy site lúc này, không phải kết quả cache (lượt build xong trong vài giây)."""
    async with res.sessionmaker() as session:
        companies = await batch_service.select_companies(
            session, company_ids=company_ids, stale_hours=stale_hours
        )
    return await batch_service.enqueue_companies(res, companies, refresh=True)


@dataclass(slots=True)
class ImportOutcome:
    rows: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    duplicates: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    enqueued: int | None = None
    already_queued: int = 0


async def import_csv(res: Resources, content: bytes, *, enqueue: bool) -> ImportOutcome:
    """Import `companies.csv` tải lên; `enqueue` thì đưa luôn các doanh nghiệp trong file vào
    queue. Ném `batch_service.CsvFormatError` nếu file sai định dạng."""
    if len(content) > MAX_UPLOAD_BYTES:
        raise batch_service.CsvFormatError("file quá lớn (tối đa 20 MB)")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise batch_service.CsvFormatError(
            "file không phải UTF-8 (Excel: Lưu thành → CSV UTF-8 (Comma delimited))"
        ) from exc
    report = batch_service.parse_companies_csv(text)
    result = await batch_service.import_companies(
        res.sessionmaker, [r.company for r in report.rows]
    )
    outcome = ImportOutcome(
        rows=len(report.rows),
        created=result.created,
        updated=result.updated,
        unchanged=result.unchanged,
        duplicates=report.duplicates,
        errors=report.errors,
        warnings=report.warnings,
    )
    if enqueue and result.company_ids:
        queued = await enqueue_companies(res, company_ids=result.company_ids)
        outcome.enqueued, outcome.already_queued = queued.enqueued, queued.already_queued
    return outcome


def demo_csv() -> bytes:
    """File CSV mẫu để tải về (đúng định dạng nạp danh sách). Thêm BOM để Excel đọc đúng UTF-8;
    nạp lại file này vẫn được (`import_csv` bỏ BOM)."""
    return codecs.BOM_UTF8 + DEMO_COMPANIES_CSV.read_bytes()


async def seed_demo_if_empty(res: Resources) -> ImportOutcome | None:
    """Bảng `companies` trống (lần đầu chạy) thì nạp 50 doanh nghiệp mẫu; có dữ liệu thì bỏ qua.
    Doanh nghiệp mới được scheduler build ở các nhịp sau."""
    async with res.sessionmaker() as session:
        count = await session.scalar(select(func.count()).select_from(Company))
    if count:
        return None
    return await import_csv(res, DEMO_COMPANIES_CSV.read_bytes(), enqueue=False)


class CompanyInputError(ValueError):
    """Doanh nghiệp nhập tay không hợp lệ (tên quá ngắn, MST / website sai)."""


@dataclass(slots=True)
class CreateOutcome:
    id: UUID
    # False: đã có doanh nghiệp này (cùng MST, hoặc cùng tên không mâu thuẫn MST / website) — chỉ
    # bổ sung website / ghi chú / MST còn trống như nạp CSV.
    created: bool
    warnings: list[str] = field(default_factory=list)
    queued: bool | None = None  # None: không yêu cầu đưa vào hàng đợi


async def create_company(
    res: Resources,
    name: str,
    *,
    tax_code: str | None = None,
    website: str | None = None,
    note: str | None = None,
    enqueue: bool = False,
) -> CreateOutcome:
    """Thêm một doanh nghiệp bằng tay — như một dòng của `companies.csv`, nhưng trường sai thì báo
    lỗi (`CompanyInputError`) thay vì bỏ qua. `enqueue` thì đưa vào hàng đợi build ngay."""
    tax, tax_warning = batch_service.clean_tax_code(tax_code)
    if tax is None and tax_code and tax_code.strip():
        raise CompanyInputError(
            f"Mã số thuế {tax_code.strip()!r} không hợp lệ (10 chữ số; chi nhánh thêm -3 chữ số)"
        )
    site, web_warning = batch_service.clean_website(website)
    if web_warning:
        raise CompanyInputError(f"Website {(website or '').strip()!r} không hợp lệ")
    try:
        company = CompanyIn(name=name, tax_code=tax, website=site, note=note)
    except ValidationError:
        raise CompanyInputError("Chưa nhập tên doanh nghiệp") from None
    if len(normalize_company_name(company.name)) < 2:
        raise CompanyInputError(f"Tên {company.name!r} quá ngắn")
    result = await batch_service.import_companies(res.sessionmaker, [company])
    outcome = CreateOutcome(
        id=result.company_ids[0],
        created=result.created > 0,
        warnings=[w for w in (tax_warning,) if w],
    )
    if enqueue:
        queued = await enqueue_companies(res, company_ids=[outcome.id])
        outcome.queued = queued.enqueued > 0
    return outcome
