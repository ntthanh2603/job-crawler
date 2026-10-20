"""Thao tác việc làm cho API: export theo bộ lọc, kiểm tra tồn tại."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from jobcrawler.database.models import Job
from jobcrawler.modules.jobs.export import CSV_COLUMNS, csv_row
from jobcrawler.modules.jobs.queries import JobFilters, job_query
from jobcrawler.pipeline.store import job_to_out
from jobcrawler.services.resources import Resources


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
