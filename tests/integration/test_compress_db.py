"""Nén cột lớn trên Postgres thật: migration nén dữ liệu cũ, huấn luyện từ điển, nén lại."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

import asyncpg
import pytest
from sqlalchemy import make_url, select, text

from jobcrawler.config import Settings
from jobcrawler.db import compress, migrate
from jobcrawler.db.compress import Codec, Kind, TrainOptions
from jobcrawler.db.models import Job, JobPosting
from jobcrawler.resources import open_resources

from ..test_compress import samples

pytestmark = pytest.mark.integration


def dsn(url: str) -> str:
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


async def test_migration_compresses_existing_rows(test_database_url: str) -> None:
    tag = uuid4().hex[:8]
    raw = {"title": "Kế toán tổng hợp", "description": "<p>Làm báo cáo thuế</p>" * 200, "n": 1}
    description = "## Mô tả công việc\n- Lập báo cáo tài chính\n" * 100
    await asyncio.to_thread(migrate.downgrade, "0003", database_url=test_database_url)
    try:
        conn = await asyncpg.connect(dsn(test_database_url))
        try:
            company_id = await conn.fetchval(
                "INSERT INTO companies (input_name) VALUES ($1) RETURNING id", f"Nén {tag}"
            )
            posting_id = await conn.fetchval(
                "INSERT INTO job_postings (source, external_id, url, raw) VALUES"
                " ('test', $1, 'https://x.vn/1', $2::jsonb) RETURNING id",
                tag,
                json.dumps(raw, ensure_ascii=False),
            )
            job_id = await conn.fetchval(
                "INSERT INTO jobs (company_id, cluster_key, title, title_normalized, company_name,"
                " description_md, requirements_md, canonical_source, confidence)"
                " VALUES ($1, $2, 'Kế toán', 'ke toan', 'X', $3, NULL, 'test', 0.9) RETURNING id",
                company_id,
                tag,
                description,
            )
        finally:
            await conn.close()

        await asyncio.to_thread(migrate.upgrade, "head", database_url=test_database_url)
        conn = await asyncpg.connect(dsn(test_database_url))
        try:
            stored = await conn.fetchrow(
                "SELECT raw, octet_length(raw) AS size FROM job_postings WHERE id = $1", posting_id
            )
            storage = await conn.fetch(
                "SELECT attname, attstorage::text FROM pg_attribute"
                " WHERE attrelid = 'jobs'::regclass"
                " AND attname IN ('description_md', 'requirements_md', 'benefits_md')"
            )
        finally:
            await conn.close()
        assert bytes(stored["raw"][:5]) == compress.header(0)
        assert stored["size"] < len(json.dumps(raw).encode()) / 5
        assert {r["attstorage"] for r in storage} == {"e"}  # EXTERNAL: Postgres không nén lại

        settings = Settings(database_url=test_database_url)
        async with open_resources(settings, db_logs=False) as res, res.sessionmaker() as s:
            posting = await s.get(JobPosting, posting_id)
            job = await s.get(Job, job_id)
        assert posting is not None
        assert posting.raw == raw
        assert job is not None
        assert (job.description_md, job.requirements_md, job.benefits_md) == (
            description,
            None,
            None,
        )

        # Hạ về 0003: dữ liệu trở lại jsonb/text như cũ.
        await asyncio.to_thread(migrate.downgrade, "0003", database_url=test_database_url)
        conn = await asyncpg.connect(dsn(test_database_url))
        try:
            back = await conn.fetchval(
                "SELECT raw::text FROM job_postings WHERE id = $1", posting_id
            )
            text_back = await conn.fetchval("SELECT description_md FROM jobs WHERE id = $1", job_id)
        finally:
            await conn.close()
        assert json.loads(back) == raw
        assert text_back == description
    finally:
        await asyncio.to_thread(migrate.upgrade, "head", database_url=test_database_url)


async def test_train_dictionary_then_recompress(integration_settings: Settings) -> None:
    source = f"compress-{uuid4().hex[:8]}"
    values = [json.loads(s) for s in samples(1200, seed=11)]
    async with open_resources(integration_settings, db_logs=False) as res:
        async with res.sessionmaker.begin() as s:
            s.add_all(
                JobPosting(source=source, external_id=str(i), url=f"https://x.vn/{i}", raw=v)
                for i, v in enumerate(values)
            )
        async with res.sessionmaker() as s:
            before = await compress.storage_stats(s, Kind.POSTING)

        options = TrainOptions(dict_size=8192, level=3, min_samples=1000, delay_seconds=0)
        now = datetime.now(UTC)
        [result] = await compress.train_dictionaries(
            res.sessionmaker, options, now=now, kinds=[Kind.POSTING]
        )
        assert result.adopted, result.reason
        assert result.dict_id is not None
        active = compress.CODEC.active(Kind.POSTING)
        assert active is not None
        assert active.id == result.dict_id

        written = await compress.recompress(res.sessionmaker, Kind.POSTING, limit=100_000)
        assert written >= len(values)
        assert await compress.recompress(res.sessionmaker, Kind.POSTING, limit=100_000) == 0

        async with res.sessionmaker() as s:
            heads = (
                await s.scalars(
                    text("SELECT substring(raw FROM 1 FOR 5) FROM job_postings WHERE source = :s"),
                    {"s": source},
                )
            ).all()
            assert {bytes(h) for h in heads} == {compress.header(result.dict_id)}
            rows = (
                await s.execute(
                    select(JobPosting.external_id, JobPosting.raw).where(
                        JobPosting.source == source
                    )
                )
            ).all()
            assert {int(e): r for e, r in rows} == dict(enumerate(values))
            after = await compress.storage_stats(s, Kind.POSTING)
            # Process khác (codec mới) nạp từ điển từ DB và đọc được.
            fresh = Codec()
            assert await compress.load_dictionaries(await s.connection(), fresh) >= 1
            blob = await s.scalar(
                text("SELECT raw FROM job_postings WHERE source = :s LIMIT 1"), {"s": source}
            )
            assert json.loads(fresh.decode(blob)) in values

        assert before.ratio is not None
        assert after.ratio is not None
        assert after.ratio > before.ratio * 1.5
        assert after.by_dict
        assert result.dict_id in after.by_dict

        # Lượt sau: từ điển còn mới thì không huấn luyện lại.
        [again] = await compress.train_dictionaries(
            res.sessionmaker, options, now=now, kinds=[Kind.POSTING]
        )
        assert not again.adopted
        assert again.reason == "từ điển còn mới"
