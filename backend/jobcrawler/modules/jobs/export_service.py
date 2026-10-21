"""Export bảng `jobs` ra JSONL (mỗi dòng một `JobOut`) hoặc CSV (các trường phẳng)."""

from __future__ import annotations

import csv
import json
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Literal
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from jobcrawler.common.logging import get_logger
from jobcrawler.common.schemas import JobOut
from jobcrawler.common.text import normalize_company_name
from jobcrawler.database.models import Company, CompanyAlias, Job
from jobcrawler.modules.jobs.status_service import open_jobs
from jobcrawler.pipeline.store import job_to_out
from jobcrawler.services.resources import Resources

log = get_logger(__name__)

ExportFormat = Literal["jsonl", "csv"]
FORMATS: tuple[ExportFormat, ...] = ("jsonl", "csv")

CSV_COLUMNS = (
    "company_id",
    "company_name",
    "title",
    "title_normalized",
    "level",
    "employment_type",
    "work_mode",
    "categories",
    "education",
    "openings",
    "provinces",
    "addresses",
    "salary_min",
    "salary_max",
    "salary_currency",
    "salary_period",
    "salary_negotiable",
    "salary_raw",
    "experience_years_min",
    "experience_years_max",
    "skills",
    "posted_at",
    "expires_at",
    "apply_url",
    "canonical_source",
    "source_urls",
    "status",
    "confidence",
    "verification_reasons",
    "first_seen_at",
    "last_seen_at",
    "description_md",
    "requirements_md",
    "benefits_md",
)
LIST_SEP = " | "
# Ô CSV bắt đầu bằng các ký tự này bị Excel / LibreOffice hiểu là công thức (tiêu đề crawl được
# `=HYPERLINK(...)` thành link chạy được, gạch đầu dòng "- ..." thành #NAME?): thêm `'` phía trước
# để luôn là chữ. JSONL giữ nguyên.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


class UnknownCompanyError(ValueError):
    pass


@dataclass(slots=True)
class ExportResult:
    path: Path
    format: ExportFormat
    count: int


def output_path(out: str | Path, fmt: ExportFormat) -> Path:
    """`out/` (thư mục) → `out/jobs.<fmt>`; `out/x.jsonl` (file) giữ nguyên."""
    path = Path(out)
    if path.suffix.lower() in (".jsonl", ".json", ".csv"):
        return path
    return path / f"jobs.{fmt}"


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value else ""


def _num(value: float | None) -> str:
    if value is None:
        return ""
    return str(int(value)) if float(value).is_integer() else str(value)


def _text_cell(value: str) -> str:
    return "'" + value if value.startswith(FORMULA_PREFIXES) else value


def csv_row(job: JobOut) -> dict[str, str]:
    salary, exp = job.salary, job.experience_years
    row = {
        "company_id": str(job.company_id),
        "company_name": job.company_name,
        "title": job.title,
        "title_normalized": job.title_normalized,
        "level": job.level.value,
        "employment_type": job.employment_type.value,
        "work_mode": job.work_mode.value,
        "categories": LIST_SEP.join(job.categories),
        "education": job.education.value,
        "openings": str(job.openings) if job.openings is not None else "",
        "provinces": LIST_SEP.join(
            dict.fromkeys(loc.province for loc in job.locations if loc.province)
        ),
        "addresses": LIST_SEP.join(loc.address_raw for loc in job.locations if loc.address_raw),
        "salary_min": _num(salary.min) if salary else "",
        "salary_max": _num(salary.max) if salary else "",
        "salary_currency": (salary.currency or "") if salary else "",
        "salary_period": salary.period.value if salary and salary.period else "",
        "salary_negotiable": "true" if salary and salary.negotiable else "false",
        "salary_raw": (salary.raw or "") if salary else "",
        "experience_years_min": _num(exp.min) if exp else "",
        "experience_years_max": _num(exp.max) if exp else "",
        "skills": LIST_SEP.join(job.skills),
        "posted_at": _iso(job.posted_at),
        "expires_at": _iso(job.expires_at),
        "apply_url": job.apply_url or "",
        "canonical_source": job.canonical_source,
        "source_urls": LIST_SEP.join(job.source_urls),
        "status": job.status.value,
        "confidence": _num(job.confidence),
        "verification_reasons": LIST_SEP.join(job.verification_reasons),
        "first_seen_at": _iso(job.first_seen_at),
        "last_seen_at": _iso(job.last_seen_at),
        "description_md": job.description_md,
        "requirements_md": job.requirements_md or "",
        "benefits_md": job.benefits_md or "",
    }
    return {key: _text_cell(value) for key, value in row.items()}


class JobWriter:
    """Ghi lần lượt từng job (CSV có dòng tiêu đề)."""

    def __init__(self, fp: IO[str], fmt: ExportFormat) -> None:
        self.fp = fp
        self.fmt = fmt
        self.count = 0
        self._csv: csv.DictWriter[str] | None = None
        if fmt == "csv":
            self._csv = csv.DictWriter(fp, fieldnames=CSV_COLUMNS, lineterminator="\n")
            self._csv.writeheader()

    def write(self, job: JobOut) -> None:
        if self._csv is not None:
            self._csv.writerow(csv_row(job))
        else:
            # ensure_ascii=False: giữ tiếng Việt đọc được trong file.
            self.fp.write(json.dumps(job.model_dump(mode="json"), ensure_ascii=False) + "\n")
        self.count += 1

    def write_all(self, jobs: Iterable[JobOut]) -> int:
        for job in jobs:
            self.write(job)
        return self.count


def _as_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


async def resolve_companies(session: AsyncSession, selectors: Sequence[str]) -> list[UUID]:
    """`--company`: id (UUID), MST, hoặc tên (khớp alias đã chuẩn hoá, hoặc một phần tên)."""
    ids: list[UUID] = []
    for selector in selectors:
        value = selector.strip()
        if (company_id := _as_uuid(value)) is not None:
            found = await session.scalars(select(Company.id).where(Company.id == company_id))
        else:
            norm = normalize_company_name(value)
            pattern = f"%{value}%"
            found = await session.scalars(
                select(Company.id)
                .outerjoin(CompanyAlias, CompanyAlias.company_id == Company.id)
                .where(
                    or_(
                        CompanyAlias.alias_norm == norm,
                        Company.tax_code == value,
                        Company.input_name.ilike(pattern),
                        Company.legal_name.ilike(pattern),
                        Company.brand_name.ilike(pattern),
                    )
                )
                .distinct()
            )
        matched = list(found)
        if not matched:
            raise UnknownCompanyError(f"không tìm thấy doanh nghiệp '{selector}'")
        ids.extend(matched)
    return list(dict.fromkeys(ids))


async def export_jobs(
    res: Resources,
    *,
    fmt: ExportFormat,
    out: str | Path,
    companies: Sequence[str] = (),
    include_inactive: bool = False,
    now: datetime | None = None,
) -> ExportResult:
    """Ghi job ra file (ghi file tạm rồi đổi tên, không để file dở). Mặc định chỉ job đang mở
    (như `status=active` của `GET /api/jobs/export`: `status_service.open_jobs`)."""
    now = now or datetime.now(UTC)
    path = output_path(out, fmt)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    async with res.sessionmaker() as session:
        stmt = select(Job).order_by(Job.company_id, Job.id)
        if companies:
            stmt = stmt.where(Job.company_id.in_(await resolve_companies(session, companies)))
        if not include_inactive:
            stmt = stmt.where(open_jobs(now))
        result = await session.stream_scalars(stmt.execution_options(yield_per=500))
        # utf-8-sig cho CSV: Excel mới nhận đúng tiếng Việt.
        encoding = "utf-8-sig" if fmt == "csv" else "utf-8"
        try:
            with tmp.open("w", encoding=encoding, newline="") as fp:
                writer = JobWriter(fp, fmt)
                async for partition in result.partitions():
                    writer.write_all(job_to_out(row, now=now) for row in partition)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    os.replace(tmp, path)
    log.info("export.done", path=str(path), format=fmt, jobs=writer.count)
    return ExportResult(path=path, format=fmt, count=writer.count)
