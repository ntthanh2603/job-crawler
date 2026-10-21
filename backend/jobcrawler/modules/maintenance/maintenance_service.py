"""Bảo trì định kỳ (cron của runner mỗi giờ, `jobcrawler db maintain`).

- Lượt chạy còn `running` quá timeout (worker chết, container bị kill) → `failed`.
- Xoá dữ liệu cũ theo lô: `failed_pages` (7 ngày), `source_fetches` và `logs` (30 ngày).
- Huấn luyện từ điển nén khi đủ dữ liệu / từ điển đã cũ, rồi nén lại dần các dòng cũ.
- Đối chiếu cột ghi sẵn trên `companies` (số job đang mở, lượt mới nhất); điền dấu vân tay và gộp
  job trùng còn sót (`dedupe_jobs`, cũng chạy tay bằng `jobcrawler db dedup`).

`reclean_jobs` (`jobcrawler db reclean`, chạy tay): áp lại bộ làm sạch nội dung lên job đã lưu.
`reclassify_jobs` (`jobcrawler db reclassify`, chạy tay): tính lại ngành nghề / học vấn / số lượng
tuyển của job đã lưu từ nội dung thô của các tin.

Thao tác bảo trì cho API: xếp cron bảo trì vào queue."""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobcrawler.common.config import Settings
from jobcrawler.common.logging import get_logger
from jobcrawler.common.schemas import EducationLevel, JobStatus, RawJob, RunStatus
from jobcrawler.database import compress
from jobcrawler.database.ids import NIL
from jobcrawler.database.models import BoardIndexRun, CompanyRun, Job, JobPosting
from jobcrawler.pipeline import store
from jobcrawler.pipeline.categories import title_category
from jobcrawler.pipeline.dedup import Cluster, merge, stored_fingerprint
from jobcrawler.pipeline.normalize import (
    NormalizedJob,
    clean_text_field,
    normalize_job,
    parse_education,
)
from jobcrawler.services.redis.locks import RedisLease
from jobcrawler.services.resources import Resources

log = get_logger(__name__)

DELETE_BATCH = 10_000
# Số job chưa có dấu vân tay xử lý mỗi lượt bảo trì (job mới luôn có sẵn; chỉ dữ liệu cũ).
DEDUP_PER_RUN = 5_000
LOCK_KEY = "jobcrawler-maintenance"
# Hạn khoá của lượt bảo trì (gia hạn mỗi LOCK_SECONDS / 3 trong lúc chạy).
LOCK_SECONDS = 60.0
# Lượt chạy doanh nghiệp còn `running` sau timeout cứng + chừng này giây là đã chết.
ABANDON_GRACE_SECONDS = 300
# Lượt index một trang: ngân sách + thời gian ghi cuối lượt + chừng này giây.
BOARD_ABANDON_GRACE_SECONDS = 3600


@dataclass(slots=True)
class MaintenanceReport:
    skipped: str | None = None  # đang có lượt bảo trì khác
    abandoned_runs: int = 0
    abandoned_board_runs: int = 0
    deleted: dict[str, int] = field(default_factory=dict)
    trained: list[compress.TrainResult] = field(default_factory=list)
    recompressed: dict[str, int] = field(default_factory=dict)
    deduplicated: int = 0
    duration_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


async def mark_abandoned(
    session: AsyncSession, settings: Settings, *, now: datetime
) -> tuple[int, int]:
    """(lượt doanh nghiệp, lượt index) còn `running` quá hạn → `failed`."""
    company_cutoff = now - timedelta(
        seconds=settings.company_timeout_seconds + ABANDON_GRACE_SECONDS
    )
    runs = await session.execute(
        update(CompanyRun)
        .where(CompanyRun.status == RunStatus.RUNNING.value, CompanyRun.started_at < company_cutoff)
        .values(
            status=RunStatus.FAILED.value,
            finished_at=now,
            error="abandoned: worker dừng giữa chừng (không ghi kết quả)",
        )
    )
    board_cutoff = now - timedelta(
        seconds=settings.board_index_budget_seconds + 900 + BOARD_ABANDON_GRACE_SECONDS
    )
    board_runs = await session.execute(
        update(BoardIndexRun)
        .where(
            BoardIndexRun.status == RunStatus.RUNNING.value,
            BoardIndexRun.started_at < board_cutoff,
        )
        .values(status=RunStatus.FAILED.value, finished_at=now, error="abandoned")
    )
    return runs.rowcount or 0, board_runs.rowcount or 0  # pyright: ignore[reportAttributeAccessIssue]


async def delete_older(
    sessionmaker: async_sessionmaker[AsyncSession], table: str, column: str, cutoff: datetime
) -> int:
    """Xoá theo lô (không khoá bảng lâu) các dòng có `column` < `cutoff`."""
    sql = text(
        f"DELETE FROM {table} WHERE id IN"
        f" (SELECT id FROM {table} WHERE {column} < :cutoff LIMIT {DELETE_BATCH})"
    )
    total = 0
    while True:
        async with sessionmaker.begin() as session:
            deleted = (await session.execute(sql, {"cutoff": cutoff})).rowcount or 0  # pyright: ignore[reportAttributeAccessIssue]
        total += deleted
        if deleted < DELETE_BATCH:
            return total


def train_options(settings: Settings) -> compress.TrainOptions:
    return compress.TrainOptions(
        dict_size=settings.compression_dict_size,
        level=settings.compression_level,
        min_samples=settings.compression_dict_min_samples,
        max_samples=settings.compression_dict_max_samples,
        delay_seconds=settings.compression_dict_delay_seconds,
        retrain_days=settings.compression_dict_retrain_days,
    )


async def refresh_company_summaries(session: AsyncSession) -> None:
    """Đối chiếu lại các cột ghi sẵn trên `companies` (số job đang mở, lượt chạy mới nhất) với dữ
    liệu thật — phòng khi một đường ghi nào đó bỏ sót. Chỉ ghi dòng lệch."""
    await session.execute(
        text(
            """
            UPDATE companies c SET active_jobs = coalesce(j.n, 0)
            FROM companies c2
            LEFT JOIN (SELECT company_id, count(*) AS n FROM jobs WHERE status = 'active'
                       GROUP BY company_id) j ON j.company_id = c2.id
            WHERE c.id = c2.id AND c.active_jobs IS DISTINCT FROM coalesce(j.n, 0)
            """
        )
    )
    await session.execute(
        text(
            """
            UPDATE companies c SET last_run_id = r.run_id, last_run_status = r.status,
                   last_run_at = r.started_at, last_run_duration_ms = r.duration_ms,
                   last_run_incomplete = r.incomplete_sources
            FROM (SELECT c3.id AS company_id, lr.*
                  FROM companies c3
                  CROSS JOIN LATERAL (
                      SELECT run_id, status, started_at, duration_ms, incomplete_sources
                      FROM company_runs WHERE company_id = c3.id
                      ORDER BY started_at DESC LIMIT 1) lr) r
            WHERE r.company_id = c.id
              AND (c.last_run_id IS DISTINCT FROM r.run_id
                   OR c.last_run_status IS DISTINCT FROM r.status
                   OR c.last_run_duration_ms IS DISTINCT FROM r.duration_ms)
            """
        )
    )


DEDUP_BATCH = 500


@dataclass(slots=True)
class DedupReport:
    scanned: int = 0
    fingerprinted: int = 0
    merged: int = 0  # job đang mở trùng nội dung với job cũ hơn → gộp vào job cũ


async def dedupe_jobs(
    sessionmaker: async_sessionmaker[AsyncSession], *, limit: int | None = None
) -> DedupReport:
    """Điền dấu vân tay cho job chưa có (lưu trước migration 0006) và gộp job đang mở trùng nội
    dung: job cũ nhất giữ lại, tin của job trùng chuyển sang nó, job trùng → `removed`. Sau khi
    chạy, index `ux_jobs_company_fingerprint_active` bảo đảm không còn job trùng."""
    report = DedupReport()
    last_id = NIL
    touched: set[UUID] = set()
    now = datetime.now(UTC)
    while limit is None or report.scanned < limit:
        async with sessionmaker.begin() as session:
            rows = (
                await session.execute(
                    select(
                        Job.id,
                        Job.company_id,
                        Job.status,
                        Job.title,
                        Job.locations,
                        Job.description_md,
                        Job.requirements_md,
                        Job.source_urls,
                        Job.first_seen_at,
                    )
                    .where(Job.fingerprint.is_(None), Job.id > last_id)
                    .order_by(Job.id)
                    .limit(DEDUP_BATCH)
                )
            ).all()
            if not rows:
                break
            for row in rows:
                report.scanned += 1
                fp = stored_fingerprint(
                    row.title, row.locations or [], row.description_md or "", row.requirements_md
                )
                keeper = None
                if row.status == JobStatus.ACTIVE.value:
                    keeper = (
                        await session.execute(
                            select(Job.id, Job.source_urls).where(
                                Job.company_id == row.company_id,
                                Job.status == JobStatus.ACTIVE.value,
                                Job.fingerprint == fp,
                                Job.id != row.id,
                            )
                        )
                    ).first()
                if keeper is None:
                    await session.execute(
                        update(Job).where(Job.id == row.id).values(fingerprint=fp)
                    )
                    report.fingerprinted += 1
                    continue
                await session.execute(
                    update(JobPosting).where(JobPosting.job_id == row.id).values(job_id=keeper.id)
                )
                await session.execute(
                    update(Job)
                    .where(Job.id == keeper.id)
                    .values(
                        source_urls=list(dict.fromkeys([*keeper.source_urls, *row.source_urls])),
                        first_seen_at=func.least(Job.first_seen_at, row.first_seen_at),
                        updated_at=now,
                    )
                )
                await session.execute(
                    update(Job)
                    .where(Job.id == row.id)
                    .values(fingerprint=fp, status=JobStatus.REMOVED.value, updated_at=now)
                )
                report.merged += 1
                touched.add(row.company_id)
            last_id = rows[-1].id
    if touched:
        async with sessionmaker.begin() as session:
            for company_id in touched:
                await store.refresh_active_jobs(session, company_id)
    log.info("jobs.deduped", **dataclasses.asdict(report))
    return report


RECLEAN_BATCH = 500


@dataclass(slots=True)
class RecleanReport:
    scanned: int = 0
    changed: int = 0


async def reclean_jobs(
    sessionmaker: async_sessionmaker[AsyncSession], *, dry_run: bool = False
) -> RecleanReport:
    """Áp lại bộ làm sạch nội dung (`normalize.clean_text_field`: che liên hệ, sửa dấu sao, link
    tương đối, email Cloudflare…) lên mô tả / yêu cầu / quyền lợi của job đã lưu — khi đổi luật
    làm sạch mà không muốn chạy lại doanh nghiệp. Nội dung đã sạch đi qua không đổi."""
    report = RecleanReport()
    last_id = NIL
    while True:
        async with sessionmaker.begin() as session:
            rows = (
                await session.execute(
                    select(
                        Job.id,
                        Job.apply_url,
                        Job.description_md,
                        Job.requirements_md,
                        Job.benefits_md,
                    )
                    .where(Job.id > last_id)
                    .order_by(Job.id)
                    .limit(RECLEAN_BATCH)
                )
            ).all()
            if not rows:
                return report
            for row in rows:
                report.scanned += 1
                values: dict[str, str] = {}
                for column in ("description_md", "requirements_md", "benefits_md"):
                    before = getattr(row, column)
                    after = clean_text_field(before, "markdown", row.apply_url) if before else ""
                    if after and after != before:
                        values[column] = after
                if values:
                    report.changed += 1
                    if not dry_run:
                        await session.execute(
                            update(Job)
                            .where(Job.id == row.id)
                            .values(**values, updated_at=func.now())
                        )
            last_id = rows[-1].id


RECLASSIFY_BATCH = 500


def reclassified(
    title: str,
    requirements: str | None,
    description: str,
    raws: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Ngành nghề / học vấn / số lượng tuyển của một job từ nội dung thô các tin đã gộp vào nó
    (tin chuẩn trước): mỗi tin qua `normalize_job` rồi gộp bằng `dedup.merge`, đúng như lúc chạy
    doanh nghiệp — học vấn lấy của tin chuẩn (trường nguồn, rồi JD của chính nó), tin khác chỉ
    điền khi tin chuẩn chưa rõ. Không như vậy thì lượt chạy sau đổi lại giá trị vừa tính."""
    now = now or datetime.now(UTC)
    members: list[NormalizedJob] = []
    for raw in raws:
        try:
            job = RawJob.model_validate(raw)
        except ValueError:  # tin rất cũ thiếu trường bắt buộc: bỏ qua
            continue
        try:
            members.append(normalize_job(job, now=now))
        except Exception:  # như `normalize_jobs`: tin lạ không vào cụm
            log.exception("reclassify.normalize_error", url=job.url)
    by_title = [c for c in [title_category(title)] if c]
    if not members:
        # Không còn tin nào (đã xoá): chỉ còn tiêu đề và nội dung đã lưu.
        education = next(
            (
                level
                for text in (requirements, description)
                if (level := parse_education(text, require_context=True))
                is not EducationLevel.UNKNOWN
            ),
            EducationLevel.UNKNOWN,
        )
        return {"categories": by_title, "education": education.value, "openings": None}
    merged = merge(Cluster(members))
    return {
        "categories": merged.categories or by_title,
        "education": merged.education.value,
        "openings": merged.openings,
    }


async def reclassify_jobs(
    sessionmaker: async_sessionmaker[AsyncSession], *, dry_run: bool = False
) -> RecleanReport:
    """Tính lại ngành nghề, học vấn, số lượng tuyển của job đã lưu từ `job_postings.raw` của các
    tin đã gắn — khi thêm / đổi luật phân loại mà không muốn chạy lại doanh nghiệp."""
    report = RecleanReport()
    last_id = NIL
    while True:
        async with sessionmaker.begin() as session:
            rows = (
                await session.execute(
                    select(
                        Job.id,
                        Job.title,
                        Job.canonical_posting_id,
                        Job.requirements_md,
                        Job.description_md,
                        Job.categories,
                        Job.education,
                        Job.openings,
                    )
                    .where(Job.id > last_id)
                    .order_by(Job.id)
                    .limit(RECLASSIFY_BATCH)
                )
            ).all()
            if not rows:
                return report
            raws: dict[UUID, list[tuple[bool, UUID, dict[str, Any]]]] = {}
            canonical = {row.id: row.canonical_posting_id for row in rows}
            postings = await session.execute(
                select(JobPosting.id, JobPosting.job_id, JobPosting.raw).where(
                    JobPosting.job_id.in_(list(canonical))
                )
            )
            for posting_id, job_id, raw in postings:
                if job_id is not None and isinstance(raw, dict):
                    first = posting_id == canonical.get(job_id)
                    raws.setdefault(job_id, []).append((not first, posting_id, raw))
            for row in rows:
                report.scanned += 1
                members = [raw for *_, raw in sorted(raws.get(row.id, []), key=lambda t: t[:2])]
                values = reclassified(row.title, row.requirements_md, row.description_md, members)
                before = {
                    "categories": list(row.categories or []),
                    "education": row.education,
                    "openings": row.openings,
                }
                if values == before:
                    continue
                report.changed += 1
                if not dry_run:
                    await session.execute(
                        update(Job).where(Job.id == row.id).values(**values, updated_at=func.now())
                    )
            last_id = rows[-1].id


async def run_maintenance(
    res: Resources,
    *,
    now: datetime | None = None,
    prune: bool = True,
    compression: bool = True,
) -> MaintenanceReport:
    report = MaintenanceReport()
    started = time.monotonic()
    now = now or datetime.now(UTC)
    s = res.settings
    # Một lượt mỗi lúc trên mọi runner (lease Redis tự gia hạn trong lúc chạy,
    # `jobcrawler.services.redis.locks`).
    lease = RedisLease(res.redis, f"{s.redis_prefix}:lock:{LOCK_KEY}", ttl=LOCK_SECONDS)
    async with lease.hold() as held:
        if not held:
            report.skipped = "đang có lượt bảo trì khác"
            return report
        async with res.sessionmaker.begin() as session:
            report.abandoned_runs, report.abandoned_board_runs = await mark_abandoned(
                session, s, now=now
            )
            await refresh_company_summaries(session)
        report.deduplicated = (await dedupe_jobs(res.sessionmaker, limit=DEDUP_PER_RUN)).merged
        if prune:
            for table, column, days in (
                ("failed_pages", "created_at", s.failed_page_retention_days),
                ("source_fetches", "fetched_at", s.source_fetch_retention_days),
                ("logs", "ts", s.log_retention_days),
            ):
                report.deleted[table] = await delete_older(
                    res.sessionmaker, table, column, now - timedelta(days=days)
                )
        if compression:
            report.trained = await compress.train_dictionaries(
                res.sessionmaker, train_options(s), now=now
            )
            budget = s.compression_recompress_rows
            for kind in compress.Kind:
                done = await compress.recompress(res.sessionmaker, kind, limit=budget)
                report.recompressed[kind.value] = done
                budget -= done
    report.duration_ms = int((time.monotonic() - started) * 1000)
    log.info(
        "maintenance.done",
        abandoned_runs=report.abandoned_runs,
        abandoned_board_runs=report.abandoned_board_runs,
        deleted=report.deleted,
        trained=[t.kind for t in report.trained if t.adopted],
        recompressed=report.recompressed,
        duration_ms=report.duration_ms,
    )
    return report


# --- thao tác cho API -----------------------------------------------------------------------------


async def enqueue_maintenance(res: Resources) -> bool:
    """Chạy cron bảo trì ngay (ở runner vai `build`)."""
    job = await res.queues.build.enqueue("maintenance_cron", key=f"{LOCK_KEY}:manual", timeout=3600)
    return job is not None
