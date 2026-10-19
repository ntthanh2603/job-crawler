"""Test tích hợp cần Postgres + Redis thật (`docker compose up -d postgres redis`).

Đặt `TEST_DATABASE_URL` (biến môi trường hoặc `.env`) trỏ tới một DB riêng: schema ở đó bị
xoá và dựng lại. Không có thì các test này bị bỏ qua — trừ khi chạy trên CI (biến `CI` bật): khi
đó thiếu DB test là lỗi, không được lặng lẽ bỏ qua cả bộ test.

Chốt chặn DB thật (không tắt được bằng cấu hình): tên DB của `TEST_DATABASE_URL` phải kết thúc
bằng `_test` và khác tên DB của `DATABASE_URL`, nếu không thì dừng ngay trước khi xoá schema.
"""

from __future__ import annotations

import asyncio
import os
from typing import NoReturn

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


TEST_DB_SUFFIX = "_test"


def _ci() -> bool:
    return os.environ.get("CI", "").strip().lower() not in ("", "0", "false", "no", "off")


def _skip_or_fail(reason: str) -> NoReturn:
    """Ngoài CI: bỏ qua; trên CI: lỗi (thiếu DB test không được làm cả bộ test "xanh")."""
    if _ci():
        pytest.fail(f"CI: {reason}")
    pytest.skip(reason)


@pytest.fixture(scope="session")
def test_database_url() -> str:
    url = _env("TEST_DATABASE_URL")
    if not url:
        _skip_or_fail("chưa đặt TEST_DATABASE_URL")
    name = make_url(url).database or ""
    if not name.endswith(TEST_DB_SUFFIX):
        pytest.fail(
            f"TEST_DATABASE_URL trỏ tới DB `{name}`: tên DB test phải kết thúc bằng"
            f" `{TEST_DB_SUFFIX}` (schema ở đó bị xoá và dựng lại)"
        )
    if name == make_url(Settings().database_url).database:
        pytest.fail("TEST_DATABASE_URL phải là DB riêng, khác DATABASE_URL")
    if not _reachable(url):
        _skip_or_fail(f"không kết nối được {make_url(url).render_as_string(hide_password=True)}")
    # Dựng lại từ đầu, đồng thời kiểm tra downgrade chạy được.
    migrate.upgrade("head", database_url=url)
    migrate.downgrade("base", database_url=url)
    migrate.upgrade("head", database_url=url)
    return url


@pytest.fixture
def integration_settings(test_database_url: str) -> Settings:
    return Settings(
        database_url=test_database_url,
        build_queue_name="jobcrawler-test-build",
        poll_queue_name="jobcrawler-test-poll",
        board_queue_name="jobcrawler-test-boards",
    )
