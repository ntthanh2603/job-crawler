"""Migration 0007 trên Postgres thật: id bigint của dữ liệu cũ → UUID, quan hệ giữ nguyên; schema
khớp models."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import asyncpg
import pytest
from alembic import command
from sqlalchemy import make_url

from jobcrawler.db import migrate
from jobcrawler.db.compress import Codec, Kind, dumps
from jobcrawler.db.migrate import alembic_config

pytestmark = pytest.mark.integration


def dsn(url: str) -> str:
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


async def test_uuid_migration_keeps_relations_and_round_trips(test_database_url: str) -> None:
    tag = uuid4().hex[:8]
    codec = Codec()  # không từ điển: giá trị hợp lệ cho các test khác đọc lại bảng
    raw = codec.encode(Kind.POSTING.value, dumps({"title": "Kế toán", "url": "https://x.vn/1"}))
    description = codec.encode(Kind.JOB_TEXT.value, "Lập báo cáo thuế".encode())
    await asyncio.to_thread(migrate.downgrade, "0006", database_url=test_database_url)
    try:
        conn = await asyncpg.connect(dsn(test_database_url))
        try:
            company = await conn.fetchval(
                "INSERT INTO companies (input_name) VALUES ($1) RETURNING id", f"UUID {tag}"
            )
            await conn.execute(
                "INSERT INTO company_aliases (company_id, alias, alias_norm) VALUES ($1, $2, $2)",
                company,
                f"uuid {tag}",
            )
            posting = await conn.fetchval(
                "INSERT INTO job_postings (source, external_id, url, raw, company_id)"
                " VALUES ('test', $1, 'https://x.vn/1', $2, $3) RETURNING id",
                tag,
                raw,
                company,
            )
            job = await conn.fetchval(
                "INSERT INTO jobs (company_id, cluster_key, title, title_normalized, company_name,"
                " description_md, canonical_source, canonical_posting_id, source_urls, status,"
                " confidence) VALUES ($1, $2, 'Kế toán', 'ke toan', 'X', $3, 'test', $4,"
                " ARRAY['https://x.vn/1'], 'removed', 0.9) RETURNING id",
                company,
                tag,
                description,
                posting,
            )
            await conn.execute("UPDATE job_postings SET job_id = $1 WHERE id = $2", job, posting)
        finally:
            await conn.close()
        assert isinstance(company, int)

        await asyncio.to_thread(migrate.upgrade, "head", database_url=test_database_url)
        conn = await asyncpg.connect(dsn(test_database_url))
        try:
            row = await conn.fetchrow(
                "SELECT c.id AS company, j.id AS job, p.id AS posting, a.company_id AS alias_of,"
                " j.canonical_posting_id, p.job_id, p.company_id AS posting_company"
                " FROM companies c JOIN jobs j ON j.company_id = c.id"
                " JOIN job_postings p ON p.id = j.canonical_posting_id"
                " JOIN company_aliases a ON a.company_id = c.id WHERE c.input_name = $1",
                f"UUID {tag}",
            )
            # Insert bằng SQL thô sau migration: id mặc định uuid_v7(), lớn hơn mọi id cũ.
            fresh = await conn.fetchval(
                "INSERT INTO companies (input_name) VALUES ($1) RETURNING id", f"UUID mới {tag}"
            )
        finally:
            await conn.close()
        assert row is not None
        assert all(isinstance(v, UUID) for v in row.values())
        assert row["alias_of"] == row["posting_company"] == row["company"]
        assert (row["canonical_posting_id"], row["job_id"]) == (row["posting"], row["job"])
        assert row["company"].version == fresh.version == 7
        assert fresh > row["company"]

        # Hạ về 0006: id cũ trở về đúng số ban đầu, id mới đánh số tiếp.
        await asyncio.to_thread(migrate.downgrade, "0006", database_url=test_database_url)
        conn = await asyncpg.connect(dsn(test_database_url))
        try:
            back = await conn.fetchrow(
                "SELECT j.id AS job, j.canonical_posting_id AS posting, j.company_id AS company"
                " FROM jobs j WHERE j.cluster_key = $1",
                tag,
            )
            fresh_back = await conn.fetchval(
                "SELECT id FROM companies WHERE input_name = $1", f"UUID mới {tag}"
            )
            next_id = await conn.fetchval(
                "INSERT INTO companies (input_name) VALUES ($1) RETURNING id", f"UUID sau {tag}"
            )
        finally:
            await conn.close()
        assert back is not None
        assert dict(back) == {"job": job, "posting": posting, "company": company}
        assert fresh_back > company
        assert next_id > fresh_back  # identity tiếp tục sau số lớn nhất
    finally:
        await asyncio.to_thread(migrate.upgrade, "head", database_url=test_database_url)


async def test_schema_matches_models(test_database_url: str) -> None:
    # Migration và models không lệch nhau (alembic autogenerate không còn thay đổi nào).
    await asyncio.to_thread(command.check, alembic_config(test_database_url))
