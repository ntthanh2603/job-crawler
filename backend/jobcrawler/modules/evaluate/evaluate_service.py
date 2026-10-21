"""`jobcrawler eval`: precision/recall trên golden set (theo doanh nghiệp và theo nguồn).

Golden set là một thư mục gồm:
- `companies.csv`: như `companies.csv` đầu vào (`name`, `tax_code`, `website`, `note`);
- `jobs.csv`: các job đúng, cột `company` (tên hoặc MST, khớp một dòng ở companies.csv) và
  `url` và/hoặc `title`.

Một job tìm được khớp một job golden khi URL trùng (một trong các URL nguồn/ứng tuyển, đã chuẩn
hoá) hoặc tiêu đề gần như trùng (rapidfuzz token_set_ratio ≥ 90 sau khi bỏ dấu); mỗi job golden
khớp tối đa một job. Theo nguồn: tin của từng nguồn (trước gộp trùng) so với cùng golden set —
precision là tỉ lệ tin của nguồn đó đúng, recall là phần golden set nguồn đó phủ được.

Chưa có golden set: lấy ngẫu nhiên N job đang mở để người kiểm tay (CSV có cột `dung` để điền),
`--reviewed file.csv` tính precision từ file đã điền.
"""

from __future__ import annotations

import csv
import io
import json
import random
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from rapidfuzz import fuzz
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from jobcrawler.common.schemas import CompanyIn, JobStatus
from jobcrawler.common.text import fold, normalize_company_name, strip_accents
from jobcrawler.common.urls import normalize_url
from jobcrawler.database.models import Company, CompanyAlias, CompanyRun, Job, JobPosting, JobReview
from jobcrawler.modules.companies import batch_service
from jobcrawler.modules.jobs.status_service import open_jobs
from jobcrawler.pipeline.kyc import MATCH_ALIAS
from jobcrawler.services.resources import Resources

TITLE_MATCH = 90.0
TARGET_PRECISION = 0.95
TARGET_RECALL = 0.85
DEFAULT_SAMPLE = 50
YES = {"y", "yes", "1", "x", "dung", "đúng", "ok", "true", "co", "có"}
NO = {"n", "no", "0", "sai", "false", "khong", "không"}


class GoldenError(ValueError):
    """Golden set thiếu file / sai cột."""


@dataclass(slots=True)
class GoldenJob:
    line: int
    company: str
    url: str | None
    title: str | None


@dataclass(slots=True)
class GoldenSet:
    companies: list[CompanyIn]
    # chỉ số trong `companies` → các job đúng
    jobs: dict[int, list[GoldenJob]]
    warnings: list[str] = field(default_factory=list)


def _folded_header(value: str) -> str:
    return " ".join(strip_accents(value).lower().replace("_", " ").split())


def load_golden(directory: str | Path) -> GoldenSet:
    root = Path(directory)
    companies_path, jobs_path = root / "companies.csv", root / "jobs.csv"
    if not companies_path.is_file() or not jobs_path.is_file():
        raise GoldenError(f"thiếu {companies_path} hoặc {jobs_path}")
    report = batch_service.read_companies_csv(companies_path)
    companies = [row.company for row in report.rows]
    golden = GoldenSet(companies=companies, jobs={}, warnings=report.errors + report.warnings)
    by_key: dict[str, int] = {}
    for i, c in enumerate(companies):
        by_key.setdefault(normalize_company_name(c.name), i)
        if c.tax_code:
            by_key.setdefault(c.tax_code, i)

    content = jobs_path.read_bytes().decode("utf-8-sig")
    try:
        dialect: Any = csv.Sniffer().sniff(content[:8192], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(content), dialect=dialect)
    fields = {_folded_header(f): f for f in reader.fieldnames or []}
    company_col = next(
        (
            fields[k]
            for k in ("company", "company name", "ten", "name", "mst", "tax code")
            if k in fields
        ),
        None,
    )
    url_col = next((fields[k] for k in ("url", "link", "job url") if k in fields), None)
    title_col = next((fields[k] for k in ("title", "tieu de", "job title") if k in fields), None)
    if company_col is None or (url_col is None and title_col is None):
        raise GoldenError("jobs.csv cần cột `company` và `url` hoặc `title`")
    for line, row in enumerate(reader, start=2):
        company = (row.get(company_col) or "").strip()
        url = (row.get(url_col) or "").strip() if url_col else ""
        title = (row.get(title_col) or "").strip() if title_col else ""
        if not company or not (url or title):
            continue
        tax, _ = batch_service.clean_tax_code(company)
        index = by_key.get(tax or "") if tax else None
        if index is None:
            index = by_key.get(normalize_company_name(company))
        if index is None:
            golden.warnings.append(f"jobs.csv dòng {line}: {company!r} không có ở companies.csv")
            continue
        golden.jobs.setdefault(index, []).append(
            GoldenJob(line=line, company=company, url=url or None, title=title or None)
        )
    return golden


# --- so khớp ----------------------------------------------------------------------------------


def url_key(url: str | None) -> str | None:
    if not url:
        return None
    normalized = normalize_url(url) or url.strip()
    return normalized.rstrip("/").lower()


@dataclass(slots=True)
class Predicted:
    """Một job (hoặc một tin của một nguồn) đem so với golden set."""

    title: str
    urls: set[str]
    source: str
    job_id: UUID | None = None


def score(golden: GoldenJob, predicted: Predicted) -> float:
    if (key := url_key(golden.url)) and key in predicted.urls:
        return 100.0
    if golden.title:
        return fuzz.token_set_ratio(fold(golden.title), fold(predicted.title))
    return 0.0


def match(
    golden: Sequence[GoldenJob], predicted: Sequence[Predicted], *, threshold: float = TITLE_MATCH
) -> list[tuple[int, int]]:
    """Cặp (chỉ số golden, chỉ số predicted) một-một, ghép cặp điểm cao trước."""
    pairs = sorted(
        (
            (-s, gi, pi)
            for gi, g in enumerate(golden)
            for pi, p in enumerate(predicted)
            if (s := score(g, p)) >= threshold
        ),
    )
    used_g: set[int] = set()
    used_p: set[int] = set()
    out: list[tuple[int, int]] = []
    for _, gi, pi in pairs:
        if gi in used_g or pi in used_p:
            continue
        used_g.add(gi)
        used_p.add(pi)
        out.append((gi, pi))
    return out


def ratio(num: int, den: int) -> float | None:
    return num / den if den else None


@dataclass(slots=True)
class CompanyEval:
    name: str
    company_id: UUID | None
    golden: int
    predicted: int
    matched: int
    run_status: str | None = None
    missing: list[str] = field(default_factory=list)  # job golden không tìm thấy
    extra: list[str] = field(default_factory=list)  # job tìm được không có trong golden

    @property
    def precision(self) -> float | None:
        return ratio(self.matched, self.predicted)

    @property
    def recall(self) -> float | None:
        return ratio(self.matched, self.golden)


@dataclass(slots=True)
class SourceEval:
    source: str
    predicted: int = 0
    matched: int = 0
    golden: int = 0  # job golden của các doanh nghiệp có tin từ nguồn này

    @property
    def precision(self) -> float | None:
        return ratio(self.matched, self.predicted)

    @property
    def recall(self) -> float | None:
        return ratio(self.matched, self.golden)


@dataclass(slots=True)
class EvalReport:
    generated_at: datetime
    companies: list[CompanyEval]
    sources: list[SourceEval]
    warnings: list[str]

    @property
    def golden(self) -> int:
        return sum(c.golden for c in self.companies)

    @property
    def predicted(self) -> int:
        return sum(c.predicted for c in self.companies)

    @property
    def matched(self) -> int:
        return sum(c.matched for c in self.companies)

    @property
    def precision(self) -> float | None:
        return ratio(self.matched, self.predicted)

    @property
    def recall(self) -> float | None:
        return ratio(self.matched, self.golden)


async def find_company_id(session: AsyncSession, company: CompanyIn) -> UUID | None:
    """Doanh nghiệp trong DB: theo MST, rồi tên chuẩn hoá (đã import/chạy)."""
    if company.tax_code:
        found = await session.scalar(select(Company.id).where(Company.tax_code == company.tax_code))
        if found is not None:
            return found
    return await session.scalar(
        select(CompanyAlias.company_id)
        .where(
            CompanyAlias.alias_norm == normalize_company_name(company.name),
            CompanyAlias.kind.is_distinct_from(MATCH_ALIAS),
        )
        .order_by(CompanyAlias.company_id)
        .limit(1)
    )


def source_kind(source: str) -> str:
    return source.split(":", 1)[0]


async def predicted_for(
    session: AsyncSession, company_id: UUID, *, include_inactive: bool = False
) -> tuple[list[Predicted], list[Predicted]]:
    """(job sau gộp, tin của từng nguồn đã được nhận) của một doanh nghiệp."""
    stmt = select(Job).where(Job.company_id == company_id)
    if not include_inactive:
        stmt = stmt.where(Job.status == JobStatus.ACTIVE.value)
    jobs = list(await session.scalars(stmt.order_by(Job.id)))
    merged = [
        Predicted(
            title=j.title,
            urls={k for u in [*j.source_urls, j.apply_url] if (k := url_key(u))},
            source=source_kind(j.canonical_source),
            job_id=j.id,
        )
        for j in jobs
    ]
    postings: list[Predicted] = []
    if jobs:
        rows = await session.execute(
            select(JobPosting.source, JobPosting.url, JobPosting.raw, JobPosting.job_id).where(
                JobPosting.job_id.in_([j.id for j in jobs]), JobPosting.accepted.is_(True)
            )
        )
        for source, url, raw, job_id in rows:
            urls = {k for u in (url, raw.get("url"), raw.get("apply_url")) if (k := url_key(u))}
            postings.append(
                Predicted(
                    title=str(raw.get("title") or ""),
                    urls=urls,
                    source=source_kind(source),
                    job_id=job_id,
                )
            )
    return merged, postings


async def evaluate(
    res: Resources, golden: GoldenSet, *, include_inactive: bool = False
) -> EvalReport:
    report = EvalReport(
        generated_at=datetime.now(UTC), companies=[], sources=[], warnings=list(golden.warnings)
    )
    by_source: dict[str, SourceEval] = {}
    async with res.sessionmaker() as session:
        for index, company in enumerate(golden.companies):
            expected = golden.jobs.get(index, [])
            company_id = await find_company_id(session, company)
            row = CompanyEval(
                name=company.name,
                company_id=company_id,
                golden=len(expected),
                predicted=0,
                matched=0,
            )
            report.companies.append(row)
            if company_id is None:
                row.run_status = "chưa import/chạy"
                row.missing = [g.title or g.url or "" for g in expected][:5]
                continue
            row.run_status = await session.scalar(
                select(CompanyRun.status)
                .where(CompanyRun.company_id == company_id)
                .order_by(CompanyRun.started_at.desc())
                .limit(1)
            )
            merged, postings = await predicted_for(
                session, company_id, include_inactive=include_inactive
            )
            pairs = match(expected, merged)
            row.predicted, row.matched = len(merged), len(pairs)
            found_g = {gi for gi, _ in pairs}
            found_p = {pi for _, pi in pairs}
            row.missing = [
                g.title or g.url or "" for gi, g in enumerate(expected) if gi not in found_g
            ][:5]
            row.extra = [p.title for pi, p in enumerate(merged) if pi not in found_p][:5]
            for kind in sorted({p.source for p in postings}):
                mine = [p for p in postings if p.source == kind]
                st = by_source.setdefault(kind, SourceEval(source=kind))
                st.predicted += len(mine)
                st.matched += len(match(expected, mine))
                st.golden += len(expected)
    report.sources = sorted(by_source.values(), key=lambda s: -s.predicted)
    return report


# --- mẫu ngẫu nhiên khi chưa có golden set ------------------------------------------------------

SAMPLE_COLUMNS = [
    "job_id",
    "company",
    "title",
    "locations",
    "salary",
    "level",
    "employment_type",
    "work_mode",
    "posted_at",
    "expires_at",
    "source",
    "url",
    "confidence",
    "reasons",
    "excerpt",
    "dung",
    "ghi_chu",
]


@dataclass(slots=True)
class SampleJob:
    job_id: UUID
    company: str
    title: str
    locations: str
    salary: str
    level: str
    employment_type: str
    work_mode: str
    posted_at: str
    expires_at: str
    source: str
    url: str
    confidence: float
    reasons: str
    excerpt: str


def _salary(job: Job) -> str:
    if job.salary_min is None and job.salary_max is None:
        return "thoả thuận" if job.salary_negotiable else (job.salary_raw or "")
    lo = f"{job.salary_min:,.0f}" if job.salary_min is not None else "?"
    hi = f"{job.salary_max:,.0f}" if job.salary_max is not None else "?"
    return f"{lo}-{hi} {job.salary_currency or ''}/{job.salary_period or ''}".strip()


async def sample_jobs(
    session: AsyncSession, *, n: int = DEFAULT_SAMPLE, seed: int | None = None
) -> list[SampleJob]:
    """N job đang mở chọn ngẫu nhiên (cố định được bằng `seed`)."""
    ids = list(await session.scalars(select(Job.id).where(open_jobs(datetime.now(UTC)))))
    picked = random.Random(seed).sample(ids, min(n, len(ids)))
    if not picked:
        return []
    jobs = {j.id: j for j in await session.scalars(select(Job).where(Job.id.in_(picked)))}
    out: list[SampleJob] = []
    for job_id in picked:
        j = jobs[job_id]
        places = ", ".join(
            dict.fromkeys(
                loc.get("province") or loc.get("address_raw") or "" for loc in j.locations
            )
        )
        out.append(
            SampleJob(
                job_id=j.id,
                company=j.company_name,
                title=j.title,
                locations=places,
                salary=_salary(j),
                level=j.level,
                employment_type=j.employment_type,
                work_mode=j.work_mode,
                posted_at=j.posted_at.date().isoformat() if j.posted_at else "",
                expires_at=j.expires_at.date().isoformat() if j.expires_at else "",
                source=j.canonical_source,
                url=j.apply_url or (j.source_urls[0] if j.source_urls else ""),
                confidence=round(j.confidence, 2),
                reasons=", ".join(j.verification_reasons),
                excerpt=" ".join(j.description_md.split())[:300],
            )
        )
    return out


def write_sample_csv(path: Path, jobs: Iterable[SampleJob]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SAMPLE_COLUMNS)
        writer.writeheader()
        for job in jobs:
            writer.writerow({**asdict(job), "dung": "", "ghi_chu": ""})


@dataclass(slots=True)
class ReviewResult:
    reviewed: int
    correct: int
    unknown: int  # dòng chưa điền / điền giá trị lạ

    @property
    def precision(self) -> float | None:
        return ratio(self.correct, self.reviewed)


def read_review(path: str | Path) -> ReviewResult:
    """Precision từ file mẫu người kiểm đã điền cột `dung` (y/n, đúng/sai, 1/0)."""
    content = Path(path).read_bytes().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(content))
    result = ReviewResult(reviewed=0, correct=0, unknown=0)
    for row in reader:
        value = (row.get("dung") or "").strip().lower()
        if value in YES or strip_accents(value) in YES:
            result.reviewed += 1
            result.correct += 1
        elif value in NO or strip_accents(value) in NO:
            result.reviewed += 1
        else:
            result.unknown += 1
    return result


# --- báo cáo ------------------------------------------------------------------------------------


def pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.1%}"


def check(value: float | None, target: float) -> str:
    if value is None:
        return "?"
    return "đạt" if value >= target else "CHƯA ĐẠT"


def markdown_report(report: EvalReport, *, perf: dict[str, Any] | None = None) -> str:
    lines = [
        f"# Báo cáo eval — {report.generated_at:%Y-%m-%d %H:%M} UTC",
        "",
        f"- Doanh nghiệp: {len(report.companies)} · job golden: {report.golden} · job tìm được:"
        f" {report.predicted} · khớp: {report.matched}",
        f"- Precision: **{pct(report.precision)}** (mục tiêu ≥ {TARGET_PRECISION:.0%}:"
        f" {check(report.precision, TARGET_PRECISION)})",
        f"- Recall: **{pct(report.recall)}** (mục tiêu ≥ {TARGET_RECALL:.0%}:"
        f" {check(report.recall, TARGET_RECALL)})",
    ]
    if perf:
        lines += [f"- {k}: {v}" for k, v in perf.items()]
    lines += [
        "",
        "## Theo doanh nghiệp",
        "",
        "| Doanh nghiệp | Lượt cuối | Golden | Tìm được | Khớp | Precision | Recall | Thiếu |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for c in report.companies:
        missing = "; ".join(c.missing).replace("|", "/")
        lines.append(
            f"| {c.name} | {c.run_status or '-'} | {c.golden} | {c.predicted} | {c.matched}"
            f" | {pct(c.precision)} | {pct(c.recall)} | {missing} |"
        )
    lines += [
        "",
        "## Theo nguồn (tin trước gộp trùng)",
        "",
        "| Nguồn | Tin | Khớp | Precision | Recall (độ phủ) |",
        "|---|---:|---:|---:|---:|",
    ]
    for s in report.sources:
        lines.append(
            f"| {s.source} | {s.predicted} | {s.matched} | {pct(s.precision)} | {pct(s.recall)} |"
        )
    if report.warnings:
        lines += ["", "## Cảnh báo", "", *[f"- {w}" for w in report.warnings]]
    return "\n".join(lines) + "\n"


def sample_markdown(jobs: Sequence[SampleJob], *, generated_at: datetime, csv_path: Path) -> str:
    lines = [
        f"# Mẫu {len(jobs)} job ngẫu nhiên để kiểm tay — {generated_at:%Y-%m-%d %H:%M} UTC",
        "",
        "Chưa có golden set (`golden/companies.csv`, `golden/jobs.csv`). Mở từng URL, điền cột"
        f" `dung` (y/n) trong `{csv_path.name}` rồi chạy"
        f" `jobcrawler eval --reviewed {csv_path.as_posix()}` để tính precision.",
        "",
    ]
    for i, j in enumerate(jobs, start=1):
        lines += [
            f"## {i}. {j.title} — {j.company}",
            "",
            f"- Nơi làm: {j.locations or '-'} · Lương: {j.salary or '-'} · {j.level} /"
            f" {j.employment_type} / {j.work_mode}",
            f"- Đăng: {j.posted_at or '-'} · Hạn: {j.expires_at or '-'} · Nguồn: {j.source}"
            f" · Độ tin cậy: {j.confidence} ({j.reasons})",
            f"- URL: {j.url}",
            f"- Trích: {j.excerpt}",
            "",
        ]
    return "\n".join(lines)


def report_json(report: EvalReport) -> str:
    data = {
        "generated_at": report.generated_at.isoformat(),
        "precision": report.precision,
        "recall": report.recall,
        "golden": report.golden,
        "predicted": report.predicted,
        "matched": report.matched,
        "companies": [
            {**asdict(c), "precision": c.precision, "recall": c.recall} for c in report.companies
        ],
        "sources": [
            {**asdict(s), "precision": s.precision, "recall": s.recall} for s in report.sources
        ],
        "warnings": report.warnings,
    }
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


async def latest_run_perf(session: AsyncSession, company_ids: Sequence[UUID]) -> dict[str, Any]:
    """p95 thời gian và tỉ lệ failed theo lượt mới nhất của các doanh nghiệp golden."""
    if not company_ids:
        return {}
    latest = (
        select(
            CompanyRun.status,
            CompanyRun.duration_ms,
            func.row_number()
            .over(partition_by=CompanyRun.company_id, order_by=CompanyRun.started_at.desc())
            .label("rn"),
        ).where(CompanyRun.company_id.in_(company_ids), CompanyRun.status != "running")
    ).subquery()
    rows = (
        await session.execute(select(latest.c.status, latest.c.duration_ms).where(latest.c.rn == 1))
    ).all()
    if not rows:
        return {}
    durations = sorted((d or 0) / 1000 for _, d in rows)
    p95 = durations[min(len(durations) - 1, round(0.95 * (len(durations) - 1)))]
    failed = sum(1 for s, _ in rows if s == "failed") / len(rows)
    return {"P95 thời gian (lượt cuối)": f"{p95:.0f} s", "Tỉ lệ thất bại (lượt cuối)": pct(failed)}


# --- kiểm tay trên giao diện web (bảng job_reviews) ----------------------------------------------


@dataclass(slots=True)
class SourceReview:
    source: str
    reviewed: int
    correct: int

    @property
    def precision(self) -> float | None:
        return ratio(self.correct, self.reviewed)


@dataclass(slots=True)
class ReviewSummary:
    reviewed: int
    correct: int
    by_source: list[SourceReview]
    unreviewed_active: int

    @property
    def precision(self) -> float | None:
        return ratio(self.correct, self.reviewed)


async def record_review(
    session: AsyncSession,
    job_id: UUID,
    *,
    correct: bool,
    note: str | None = None,
    reviewer: str | None = None,
) -> None:
    """Ghi (hoặc sửa) đánh giá đúng/sai của một job."""
    values = {
        "correct": correct,
        "note": (note or "").strip() or None,
        "reviewer": reviewer,
        "reviewed_at": func.now(),
    }
    stmt = pg_insert(JobReview).values(job_id=job_id, **values)
    await session.execute(
        stmt.on_conflict_do_update(index_elements=[JobReview.job_id], set_=values)
    )


async def delete_review(session: AsyncSession, job_id: UUID) -> None:
    await session.execute(delete(JobReview).where(JobReview.job_id == job_id))


async def review_summary(session: AsyncSession) -> ReviewSummary:
    kind = func.split_part(Job.canonical_source, ":", 1)
    rows = await session.execute(
        select(kind, func.count(), func.count().filter(JobReview.correct.is_(True)))
        .join(Job, Job.id == JobReview.job_id)
        .group_by(kind)
        .order_by(func.count().desc())
    )
    by_source = [SourceReview(source=s, reviewed=n, correct=c) for s, n, c in rows]
    unreviewed = await session.scalar(
        select(func.count())
        .select_from(Job)
        .outerjoin(JobReview, JobReview.job_id == Job.id)
        .where(open_jobs(datetime.now(UTC)), JobReview.id.is_(None))
    )
    return ReviewSummary(
        reviewed=sum(s.reviewed for s in by_source),
        correct=sum(s.correct for s in by_source),
        by_source=by_source,
        unreviewed_active=int(unreviewed or 0),
    )


async def next_unreviewed(session: AsyncSession, *, exclude: UUID | None = None) -> UUID | None:
    """Id một job đang mở chưa kiểm, chọn ngẫu nhiên."""
    stmt = (
        select(Job.id)
        .outerjoin(JobReview, JobReview.job_id == Job.id)
        .where(open_jobs(datetime.now(UTC)), JobReview.id.is_(None))
        .order_by(func.random())
        .limit(1)
    )
    if exclude is not None:
        stmt = stmt.where(Job.id != exclude)
    return await session.scalar(stmt)
