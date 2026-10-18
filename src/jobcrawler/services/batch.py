"""Nạp danh sách doanh nghiệp (`jobcrawler import`) và đưa vào queue (`jobcrawler enqueue`)."""

from __future__ import annotations

import asyncio
import csv
import io
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from saq import Queue
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobcrawler.config import Settings
from jobcrawler.db.models import Company, CompanyAlias, CompanyRun
from jobcrawler.logging import get_logger
from jobcrawler.net.urls import parse_domain_input
from jobcrawler.pipeline import kyc
from jobcrawler.pipeline.company_facts import valid_tax_code
from jobcrawler.schemas import CompanyIn, RunStatus
from jobcrawler.text import normalize_company_name, strip_accents

log = get_logger(__name__)

# Tên cột chấp nhận (so sau khi viết thường, bỏ dấu).
_HEADERS = {
    "name": ("name", "ten", "ten doanh nghiep", "ten cong ty", "company", "company name"),
    "tax_code": ("tax_code", "tax code", "taxcode", "mst", "ma so thue"),
    "website": ("website", "web", "domain", "url"),
    "note": ("note", "notes", "ghi chu"),
}
IMPORT_CHUNK = 500
ENQUEUE_CONCURRENCY = 50
# Lượt chạy xong (có kết quả) — dùng cho `--stale-hours`; `failed` coi như chưa chạy.
DONE_STATUSES = (RunStatus.OK.value, RunStatus.PARTIAL.value, RunStatus.AMBIGUOUS.value)


class CsvFormatError(ValueError):
    """File không đọc được (sai mã hoá, thiếu cột `name`)."""


@dataclass(slots=True)
class CsvRow:
    line: int
    company: CompanyIn


@dataclass(slots=True)
class CsvReport:
    rows: list[CsvRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)  # dòng bị bỏ
    warnings: list[str] = field(default_factory=list)  # dòng vẫn nhận, đã sửa/bỏ một trường
    duplicates: int = 0  # dòng trùng dòng trước trong file


def _header_key(value: str) -> str | None:
    folded = " ".join(strip_accents(value).lower().replace("_", " ").split())
    for key, names in _HEADERS.items():
        if folded in {n.replace("_", " ") for n in names}:
            return key
    return None


def clean_tax_code(value: str | None) -> tuple[str | None, str | None]:
    """(MST chuẩn, cảnh báo). Excel hay làm mất số 0 đầu ("0101234567" → 101234567)."""
    if not value or not value.strip():
        return None, None
    digits = re.sub(r"[\s.]", "", value)
    if re.fullmatch(r"\d{9}|\d{9}-\d{3}|\d{12}", digits):
        digits = "0" + digits
        fixed = valid_tax_code(digits[:10] + "-" + digits[10:] if len(digits) == 13 else digits)
        return fixed, f"MST {value!r} thiếu số 0 đầu (Excel?), dùng {fixed}"
    if re.fullmatch(r"\d{13}", digits):
        digits = f"{digits[:10]}-{digits[10:]}"
    tax = valid_tax_code(digits)
    if tax is None:
        return None, f"MST {value!r} không hợp lệ, bỏ qua"
    return tax, None


def clean_website(value: str | None) -> tuple[str | None, str | None]:
    if not value or not value.strip():
        return None, None
    value = value.strip()
    try:
        host = parse_domain_input(value)
    except ValueError:
        host = ""
    if " " in value or "." not in host:
        return None, f"website {value!r} không hợp lệ, bỏ qua"
    return value, None


def parse_companies_csv(content: str) -> CsvReport:
    """Đọc `companies.csv`: cột `name` (bắt buộc), `tax_code`, `website`, `note`; dấu phân
    cách `,` `;` hoặc tab; dòng trùng (cùng tên chuẩn hoá + MST) chỉ lấy dòng đầu."""
    report = CsvReport()
    sample = content[:8192]
    try:
        dialect: Any = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(content), dialect)
    header = next(reader, None)
    if header is None:
        raise CsvFormatError("file rỗng")
    columns = {i: key for i, h in enumerate(header) if (key := _header_key(h))}
    if "name" not in columns.values():
        raise CsvFormatError(f"thiếu cột `name` (dòng tiêu đề: {', '.join(header)})")
    seen: set[tuple[str, str | None]] = set()
    for line, cells in enumerate(reader, start=2):
        if not any(c.strip() for c in cells):
            continue
        values = {key: cells[i] for i, key in columns.items() if i < len(cells)}
        tax, tax_warning = clean_tax_code(values.get("tax_code"))
        website, web_warning = clean_website(values.get("website"))
        try:
            company = CompanyIn(
                name=values.get("name", ""), tax_code=tax, website=website, note=values.get("note")
            )
        except ValidationError:
            report.errors.append(f"dòng {line}: thiếu tên doanh nghiệp")
            continue
        norm = normalize_company_name(company.name)
        if len(norm) < 2:
            report.errors.append(f"dòng {line}: tên {company.name!r} quá ngắn")
            continue
        report.warnings += [f"dòng {line}: {w}" for w in (tax_warning, web_warning) if w]
        if (norm, tax) in seen:
            report.duplicates += 1
            continue
        seen.add((norm, tax))
        report.rows.append(CsvRow(line=line, company=company))
    return report


def read_companies_csv(path: str | Path) -> CsvReport:
    raw = Path(path).read_bytes()
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvFormatError(
            "file không phải UTF-8 (Excel: Lưu thành → CSV UTF-8 (Comma delimited))"
        ) from exc
    return parse_companies_csv(content)


# --- import ---------------------------------------------------------------------------------


@dataclass(slots=True)
class ImportResult:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    # Id doanh nghiệp theo thứ tự dòng trong file (không trùng).
    company_ids: list[UUID] = field(default_factory=list)


def _pick(candidates: Iterable[Company], company_in: CompanyIn) -> Company | None:
    """Bản ghi cùng tên không mâu thuẫn MST/website; ưu tiên cùng MST, rồi đã xác minh."""
    ok = [c for c in candidates if kyc.is_compatible(c, company_in)]
    ok.sort(
        key=lambda c: (
            not (company_in.tax_code and c.tax_code == company_in.tax_code),
            c.kyc_status != "verified",
            c.id,
        )
    )
    return ok[0] if ok else None


async def _import_chunk(
    session: AsyncSession, rows: Sequence[CompanyIn], result: ImportResult
) -> None:
    norms = {normalize_company_name(r.name) for r in rows}
    taxes = {r.tax_code for r in rows if r.tax_code}
    # Khoá như `kyc.resolve_company` để worker đang chạy không tạo trùng cùng lúc.
    for key in sorted(norms | {f"mst:{t}" for t in taxes}):
        await kyc._lock(session, key)  # pyright: ignore[reportPrivateUsage]
    by_tax = {
        c.tax_code: c
        for c in await session.scalars(select(Company).where(Company.tax_code.in_(taxes)))
    }
    by_name: dict[str, list[Company]] = {}
    for company, norm in await session.execute(
        select(Company, CompanyAlias.alias_norm)
        .join(CompanyAlias, CompanyAlias.company_id == Company.id)
        .where(
            CompanyAlias.alias_norm.in_(norms),
            CompanyAlias.kind.is_distinct_from(kyc.MATCH_ALIAS),
        )
    ):
        by_name.setdefault(norm, []).append(company)

    for row in rows:
        norm = normalize_company_name(row.name)
        company = (by_tax.get(row.tax_code) if row.tax_code else None) or _pick(
            by_name.get(norm, []), row
        )
        if company is None:
            company = Company(
                input_name=row.name,
                tax_code=row.tax_code,
                input_website=row.website,
                note=row.note,
            )
            session.add(company)
            await session.flush()
            result.created += 1
            by_name.setdefault(norm, []).append(company)
            if row.tax_code:
                by_tax[row.tax_code] = company
        else:
            changed = False
            if row.website and company.input_website != row.website:
                company.input_website, changed = row.website, True
            if row.note and company.note != row.note:
                company.note, changed = row.note, True
            if row.tax_code and not company.tax_code:
                company.tax_code, changed = row.tax_code, True
                by_tax[row.tax_code] = company
            result.updated += changed
            result.unchanged += not changed
        await session.execute(
            pg_insert(CompanyAlias)
            .values(kyc.alias_rows(company.id, row.name, "input"))
            .on_conflict_do_nothing(index_elements=["company_id", "alias_norm"])
        )
        if company.id not in result.company_ids:
            result.company_ids.append(company.id)


async def import_companies(
    sessionmaker: async_sessionmaker[AsyncSession], rows: Sequence[CompanyIn]
) -> ImportResult:
    """Tạo/cập nhật `companies` + alias tên đầu vào (chưa KYC). Chạy lại không tạo trùng:
    khớp theo MST rồi theo tên chuẩn hoá (không mâu thuẫn MST/website)."""
    result = ImportResult()
    for start in range(0, len(rows), IMPORT_CHUNK):
        async with sessionmaker.begin() as session:
            await _import_chunk(session, rows[start : start + IMPORT_CHUNK], result)
    log.info(
        "import.done",
        created=result.created,
        updated=result.updated,
        unchanged=result.unchanged,
    )
    return result


# --- enqueue --------------------------------------------------------------------------------


@dataclass(slots=True)
class EnqueueResult:
    selected: int = 0
    enqueued: int = 0
    already_queued: int = 0  # đã có job cùng khoá đang chờ/chạy


async def select_companies(
    session: AsyncSession,
    *,
    company_ids: Sequence[UUID] | None = None,
    stale_hours: float | None = None,
    limit: int | None = None,
    now: datetime | None = None,
) -> list[Company]:
    """Doanh nghiệp cần chạy: theo danh sách id (None = tất cả), chỉ lấy doanh nghiệp chưa có
    lượt chạy xong trong `stale_hours` giờ qua nếu có."""
    stmt = select(Company)
    if company_ids is not None:
        stmt = stmt.where(Company.id.in_(company_ids))
    if stale_hours is not None:
        cutoff = (now or datetime.now(UTC)) - timedelta(hours=stale_hours)
        last = (
            select(CompanyRun.company_id, func.max(CompanyRun.finished_at).label("finished"))
            .where(CompanyRun.status.in_(DONE_STATUSES))
            .group_by(CompanyRun.company_id)
            .subquery()
        )
        stmt = stmt.outerjoin(last, last.c.company_id == Company.id).where(
            or_(last.c.finished.is_(None), last.c.finished < cutoff)
        )
    companies = list(await session.scalars(stmt.order_by(Company.id)))
    if company_ids is not None:
        order = {cid: i for i, cid in enumerate(company_ids)}
        companies.sort(key=lambda c: order.get(c.id, len(order)))
    return companies[:limit] if limit is not None else companies


def job_key(company: Company) -> str:
    """Khoá job SAQ: một doanh nghiệp chỉ có một job đang chờ/chạy. Thêm MST để hai pháp nhân
    trùng tên chuẩn hoá ("Công ty TNHH ABC" và "Công ty CP ABC") không chặn nhau."""
    key = f"company:{normalize_company_name(company.input_name)}"
    return f"{key}:{company.tax_code}" if company.tax_code else key


def job_kwargs(company: Company) -> dict[str, Any]:
    return {
        "company_id": str(company.id),  # tham số job SAQ là JSON
        "name": company.input_name,
        "tax_code": company.tax_code,
        "website": company.input_website,
        "note": company.note,
    }


async def enqueue_companies(
    queue: Queue, companies: Sequence[Company], settings: Settings
) -> EnqueueResult:
    """Mỗi doanh nghiệp một job `process_company` (timeout cứng COMPANY_TIMEOUT_SECONDS, chạy
    lại tối đa COMPANY_JOB_RETRIES lần khi worker chết)."""
    result = EnqueueResult(selected=len(companies))
    sem = asyncio.Semaphore(ENQUEUE_CONCURRENCY)

    async def one(company: Company) -> bool:
        async with sem:
            job = await queue.enqueue(
                "process_company",
                key=job_key(company),
                timeout=settings.company_timeout_seconds,
                retries=settings.company_job_retries,
                **job_kwargs(company),
            )
        return job is not None

    for ok in await asyncio.gather(*(one(c) for c in companies)):
        result.enqueued += ok
        result.already_queued += not ok
    log.info(
        "enqueue.done",
        selected=result.selected,
        enqueued=result.enqueued,
        already_queued=result.already_queued,
    )
    return result
