"""Thao tác dùng chung cho trang web và API: đưa job vào queue, import CSV, export."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from jobcrawler.db.models import Job
from jobcrawler.entrypoints.runner import board_job_timeout
from jobcrawler.pipeline.store import job_to_out
from jobcrawler.resources import Resources
from jobcrawler.schemas import RunTrigger
from jobcrawler.services import batch, crawl, schedule, sla
from jobcrawler.services.export import CSV_COLUMNS, csv_row
from jobcrawler.services.maintenance import LOCK_KEY
from jobcrawler.sources import boards
from jobcrawler.web.queries import JobFilters, job_query

MAX_UPLOAD_BYTES = 20 * 1024 * 1024


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


def board_names() -> list[str]:
    return list(boards.registry())


async def enqueue_board(res: Resources, name: str) -> bool:
    """Đưa một trang tuyển dụng vào queue index (runner vai `boards`). False nếu trang đó đang
    chờ/đang index."""
    if name not in boards.registry():
        raise KeyError(name)
    job = await res.queues.boards.enqueue(
        "index_board",
        source=name,
        key=f"board:{name}",
        timeout=board_job_timeout(res.settings),
    )
    return job is not None


async def enqueue_build(res: Resources, company_id: UUID) -> bool:
    """Build lại bộ khung một doanh nghiệp ngay (tìm nguồn lại). False nếu đang chờ / chạy."""
    return (
        await crawl.enqueue_builds(res, [company_id], trigger=RunTrigger.MANUAL, refresh=True) > 0
    )


async def enqueue_poll(res: Resources, company_id: UUID) -> bool:
    """Poll một doanh nghiệp ngay theo bộ khung. False nếu đang chờ / chạy."""
    return await crawl.enqueue_polls(res, [company_id], trigger=RunTrigger.MANUAL) > 0


async def crawl_status(res: Resources, *, minutes: int = 60) -> dict[str, object]:
    """Số đo độ trễ phát hiện + độ tươi + khung giờ, cho trang Tổng quan."""
    s = res.settings
    now = datetime.now(UTC)
    stats = await sla.poll_stats(res.redis, s.redis_prefix, minutes=minutes)
    async with res.sessionmaker() as session:
        fresh = await sla.freshness(session, now)
    build_window = schedule.build_window(s)
    poll_window = schedule.poll_window(s)
    return {
        "target_seconds": sla.TARGET_SECONDS,
        "poll_interval_seconds": s.poll_interval_seconds,
        "polls": stats.as_dict(),
        "freshness": fresh.as_dict(),
        "windows": {
            "timezone": s.crawl_timezone,
            "build": s.build_window,
            "poll": s.poll_window,
            "in_build": build_window.contains(now),
            "in_poll": poll_window.contains(now),
            "next_build_start": build_window.next_start(now),
            "next_poll_start": poll_window.next_start(now),
        },
    }


async def enqueue_maintenance(res: Resources) -> bool:
    """Chạy cron bảo trì ngay (ở runner vai `build`)."""
    job = await res.queues.build.enqueue("maintenance_cron", key=f"{LOCK_KEY}:manual", timeout=3600)
    return job is not None


async def export_stream(res: Resources, filters: JobFilters, fmt: str) -> AsyncIterator[str]:
    """Job theo bộ lọc hiện tại, ghi dần (JSONL: mỗi dòng một JobOut; CSV có BOM cho Excel)."""
    now = datetime.now(UTC)
    async with res.sessionmaker() as session:
        stmt = job_query(filters).order_by(Job.company_id, Job.id)
        result = await session.stream_scalars(stmt.execution_options(yield_per=500))
        buffer = io.StringIO()
        writer: csv.DictWriter[str] | None = None
        if fmt == "csv":
            buffer.write("﻿")
            writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, lineterminator="\n")
            writer.writeheader()
        async for partition in result.partitions():
            for row in partition:
                job = job_to_out(row, now=now)
                if writer is not None:
                    writer.writerow(csv_row(job))
                else:
                    buffer.write(json.dumps(job.model_dump(mode="json"), ensure_ascii=False))
                    buffer.write("\n")
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate()
        if buffer.tell():
            yield buffer.getvalue()


async def job_exists(res: Resources, job_id: UUID) -> bool:
    async with res.sessionmaker() as session:
        return await session.scalar(select(Job.id).where(Job.id == job_id)) is not None
