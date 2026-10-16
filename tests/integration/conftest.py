"""Test tích hợp cần Postgres + Redis thật (`docker compose up -d postgres redis`).

Đặt `TEST_DATABASE_URL` (biến môi trường hoặc `.env`) trỏ tới một DB riêng: schema ở đó bị
xoá và dựng lại. Không có thì các test này bị bỏ qua.
"""

from __future__ import annotations

import asyncio
import os

import asyncpg
import pytest
from dotenv import dotenv_values
from sqlalchemy import make_url

from jobcrawler.config import Settings
from jobcrawler.db import migrate

pytestmark = pytest.mark.integration


def _env(name: str) -> str | None:
    return os.environ.get(name) or dotenv_values(".env").get(name) or None


def _reachable(url: str) -> bool:
    dsn = make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)

    async def ping() -> None:
        conn = await asyncpg.connect(dsn, timeout=3)
        await conn.close()

    try:
        asyncio.run(ping())
    except Exception:
        return False
    return True


@pytest.fixture(scope="session")
def test_database_url() -> str:
    url = _env("TEST_DATABASE_URL")
    if not url:
        pytest.skip("chưa đặt TEST_DATABASE_URL")
    if make_url(url).database == make_url(Settings().database_url).database:
        pytest.fail("TEST_DATABASE_URL phải là DB riêng, khác DATABASE_URL")
    if not _reachable(url):
        pytest.skip(f"không kết nối được {make_url(url).render_as_string(hide_password=True)}")
    # Dựng lại từ đầu, đồng thời kiểm tra downgrade chạy được.
    migrate.upgrade("head", database_url=url)
    migrate.downgrade("base", database_url=url)
    migrate.upgrade("head", database_url=url)
    return url


@pytest.fixture
def integration_settings(test_database_url: str) -> Settings:
    return Settings(database_url=test_database_url, queue_name="jobcrawler-test")
