"""Migration (0001 schema ban đầu, 0002 nhật ký thao tác) trên Postgres thật: khớp models, hàm
`uuid_v7()`, cột nén lưu `EXTERNAL`."""

from __future__ import annotations

import asyncio
from uuid import UUID

import asyncpg
import pytest
from alembic import command
from sqlalchemy import make_url

from jobcrawler.database.migrate import alembic_config

pytestmark = pytest.mark.integration


def dsn(url: str) -> str:
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


async def test_schema_matches_models(test_database_url: str) -> None:
    # Migration và models không lệch nhau (alembic autogenerate không còn thay đổi nào).
    await asyncio.to_thread(command.check, alembic_config(test_database_url))


async def test_uuid_v7_default_and_compressed_storage(test_database_url: str) -> None:
    conn = await asyncpg.connect(dsn(test_database_url))
    try:
        a, b = await conn.fetchval("SELECT uuid_v7()"), await conn.fetchval("SELECT uuid_v7()")
        assert UUID(str(a)).version == 7
        assert str(a) < str(b) or str(a)[:13] == str(b)[:13]  # tăng theo thời gian (ms)
        storage = dict(
            await conn.fetch(
                """SELECT c.relname || '.' || a.attname, a.attstorage::text
                   FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
                   WHERE c.relname IN ('job_postings', 'jobs', 'failed_pages')
                     AND a.attname IN ('raw', 'description_md', 'requirements_md',
                                       'benefits_md', 'html_zstd')"""
            )
        )
        assert set(storage.values()) == {"e"}  # EXTERNAL: không nén lại giá trị đã nén zstd
        assert len(storage) == 5
        fill = await conn.fetchval(
            "SELECT reloptions FROM pg_class WHERE relname = 'company_schedules'"
        )
        assert "fillfactor=70" in fill
    finally:
        await conn.close()
