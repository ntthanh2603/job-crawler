"""Bảo trì định kỳ (cron SAQ mỗi giờ, `jobcrawler db maintain`).

- Lượt chạy còn `running` quá timeout (worker chết, container bị kill) → `failed`.
- Xoá dữ liệu cũ theo lô: `failed_pages` (7 ngày), `source_fetches` và `logs` (30 ngày).
- Huấn luyện từ điển nén khi đủ dữ liệu / từ điển đã cũ, rồi nén lại dần các dòng cũ.
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
from jobcrawler.db.models import BoardIndexRun, CompanyRun
from jobcrawler.logging import get_logger
from jobcrawler.resources import Resources
from jobcrawler.schemas import RunStatus

log = get_logger(__name__)

DELETE_BATCH = 10_000
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
