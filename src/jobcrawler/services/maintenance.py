"""Bảo trì định kỳ (cron SAQ mỗi giờ, `jobcrawler db maintain`).

- Lượt chạy còn `running` quá timeout (worker chết, container bị kill) → `failed`.
- Xoá dữ liệu cũ theo lô: `failed_pages` (7 ngày), `source_fetches` và `logs` (30 ngày).
- Huấn luyện từ điển nén khi đủ dữ liệu / từ điển đã cũ, rồi nén lại dần các dòng cũ.
- Đối chiếu cột ghi sẵn trên `companies` (số job đang mở, lượt mới nhất); điền dấu vân tay và gộp
  job trùng còn sót (`dedupe_jobs`, cũng chạy tay bằng `jobcrawler db dedup`).

`reclean_jobs` (`jobcrawler db reclean`, chạy tay): áp lại bộ làm sạch nội dung lên job đã lưu.
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobcrawler.config import Settings
from jobcrawler.db import compress
from jobcrawler.db.models import BoardIndexRun, CompanyRun, Job, JobPosting
from jobcrawler.logging import get_logger
from jobcrawler.pipeline import store
from jobcrawler.pipeline.dedup import stored_fingerprint
from jobcrawler.pipeline.normalize import clean_text_field
from jobcrawler.resources import Resources
from jobcrawler.schemas import JobStatus, RunStatus

log = get_logger(__name__)

DELETE_BATCH = 10_000
# Số job chưa có dấu vân tay xử lý mỗi lượt bảo trì (job mới luôn có sẵn; chỉ dữ liệu cũ).
DEDUP_PER_RUN = 5_000
LOCK_KEY = "jobcrawler-maintenance"
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
    last_id = 0
    touched: set[int] = set()
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
    last_id = 0
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
    async with res.engine.connect() as lock_conn:
        got = await lock_conn.scalar(select(func.pg_try_advisory_lock(func.hashtext(LOCK_KEY))))
        await lock_conn.commit()
        if not got:
            report.skipped = "đang có lượt bảo trì khác"
            return report
        try:
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
        finally:
            await lock_conn.execute(select(func.pg_advisory_unlock(func.hashtext(LOCK_KEY))))
            await lock_conn.commit()
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
