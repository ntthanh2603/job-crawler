"""Thao tác doanh nghiệp cho API và `jobcrawler serve`: import CSV, thêm một doanh nghiệp, đưa
vào queue, nạp danh sách mẫu khi DB trống."""

from __future__ import annotations

import codecs
from collections.abc import Sequence
from dataclasses import dataclass, field
from importlib.resources import files
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, select

from jobcrawler.common.schemas import CompanyIn
from jobcrawler.common.text import normalize_company_name
from jobcrawler.database.models import Company
from jobcrawler.modules.companies import batch
from jobcrawler.services.resources import Resources

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
# 50 doanh nghiệp lớn của Việt Nam (tên + website) để thử hệ thống: nạp khi DB trống
# (`seed_demo_if_empty`), tải về làm file mẫu ở trang Doanh nghiệp (`demo_csv`).
DEMO_COMPANIES_CSV = files("jobcrawler").joinpath("data", "demo_companies.csv")


async def enqueue_companies(
    res: Resources,
    *,
    company_ids: Sequence[UUID] | None = None,
    stale_hours: float | None = None,
) -> batch.EnqueueResult:
    """`company_ids` None = mọi doanh nghiệp (lọc `stale_hours` nếu có)."""
    async with res.sessionmaker() as session:
        companies = await batch.select_companies(
            session, company_ids=company_ids, stale_hours=stale_hours
        )
    return await batch.enqueue_companies(res, companies)


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
    queue. Ném `batch.CsvFormatError` nếu file sai định dạng."""
    if len(content) > MAX_UPLOAD_BYTES:
        raise batch.CsvFormatError("file quá lớn (tối đa 20 MB)")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise batch.CsvFormatError(
            "file không phải UTF-8 (Excel: Lưu thành → CSV UTF-8 (Comma delimited))"
        ) from exc
    report = batch.parse_companies_csv(text)
    result = await batch.import_companies(res.sessionmaker, [r.company for r in report.rows])
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
    tax, tax_warning = batch.clean_tax_code(tax_code)
    if tax is None and tax_code and tax_code.strip():
        raise CompanyInputError(
            f"Mã số thuế {tax_code.strip()!r} không hợp lệ (10 chữ số; chi nhánh thêm -3 chữ số)"
        )
    site, web_warning = batch.clean_website(website)
    if web_warning:
        raise CompanyInputError(f"Website {(website or '').strip()!r} không hợp lệ")
    try:
        company = CompanyIn(name=name, tax_code=tax, website=site, note=note)
    except ValidationError:
        raise CompanyInputError("Chưa nhập tên doanh nghiệp") from None
    if len(normalize_company_name(company.name)) < 2:
        raise CompanyInputError(f"Tên {company.name!r} quá ngắn")
    result = await batch.import_companies(res.sessionmaker, [company])
    outcome = CreateOutcome(
        id=result.company_ids[0],
        created=result.created > 0,
        warnings=[w for w in (tax_warning,) if w],
    )
    if enqueue:
        queued = await enqueue_companies(res, company_ids=[outcome.id])
        outcome.queued = queued.enqueued > 0
    return outcome
