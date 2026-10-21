"""Phân trang danh sách (`common/pagination.py`, như open-asm): tham số, ORDER BY, số trang."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from jobcrawler.common.pagination import (
    MAX_LIMIT,
    GetManyQuery,
    Page,
    SortOrder,
    ordering,
)
from jobcrawler.database.models import Job


def _sql(order: list) -> str:
    stmt = select(Job.id).order_by(*order)
    return str(stmt.compile(dialect=postgresql.dialect())).split("ORDER BY", 1)[1].strip()


def test_ordering_keeps_index_shape_for_not_null_columns() -> None:
    # Cột NOT NULL: không ghi NULLS (khớp index `first_seen_at DESC, id DESC`, cả khi quét ngược).
    assert _sql(ordering(Job.first_seen_at, SortOrder.DESC, Job.id)) == (
        "jobs.first_seen_at DESC, jobs.id DESC"
    )
    assert _sql(ordering(Job.first_seen_at, SortOrder.ASC, Job.id)) == (
        "jobs.first_seen_at ASC, jobs.id ASC"
    )


def test_ordering_puts_nulls_last_both_ways_for_nullable_columns() -> None:
    assert _sql(ordering(Job.posted_at, SortOrder.DESC, Job.id, nullable=True)) == (
        "jobs.posted_at DESC NULLS LAST, jobs.id DESC"
    )
    assert _sql(ordering(Job.posted_at, SortOrder.ASC, Job.id, nullable=True)) == (
        "jobs.posted_at ASC NULLS LAST, jobs.id ASC"
    )


@pytest.mark.parametrize(
    ("total", "page", "limit", "pages", "has_next"),
    [(0, 1, 10, 1, False), (10, 1, 10, 1, False), (11, 1, 10, 2, True), (25, 3, 10, 3, False)],
)
def test_page_count_and_next(total: int, page: int, limit: int, pages: int, has_next: bool) -> None:
    p = Page(items=[], total=total, page=page, limit=limit)
    assert (p.page_count, p.has_next_page) == (pages, has_next)


def test_get_many_query_defaults_and_bounds() -> None:
    q = GetManyQuery()
    assert (q.search, q.page, q.limit, q.sortOrder) == ("", 1, 10, SortOrder.DESC)
    assert GetManyQuery.model_validate({"sortOrder": "ASC", "limit": "50"}).limit == 50
    for bad in ({"page": 0}, {"limit": 0}, {"limit": MAX_LIMIT + 1}, {"sortOrder": "asc"}):
        with pytest.raises(ValidationError):
            GetManyQuery.model_validate(bad)
