"""Truy vấn lịch sử index trang tuyển dụng cho giao diện."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jobcrawler.common.pagination import Page, paginate
from jobcrawler.database.models import BoardIndexRun


async def board_runs(
    session: AsyncSession, page: int | None = 1, *, mode: str | None = None
) -> Page[BoardIndexRun]:
    """Lịch sử index trang tuyển dụng; `mode`: `full` (đủ, đêm) | `tail` (đuôi, ngày)."""
    stmt = select(BoardIndexRun)
    if mode == "tail":
        stmt = stmt.where(BoardIndexRun.stats["mode"].astext == "tail")
    elif mode == "full":
        stmt = stmt.where(func.coalesce(BoardIndexRun.stats["mode"].astext, "full") == "full")
    return await paginate(session, stmt, page, order_by=[BoardIndexRun.started_at.desc()], size=20)
