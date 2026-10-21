"""Bộ lọc danh sách / export việc làm (dựng SQL, không cần DB; chạy thật ở test tích hợp)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from jobcrawler.common.pagination import SortOrder
from jobcrawler.database.models import JOB_SALARY_MONTH_SQL
from jobcrawler.modules.jobs import status_service
from jobcrawler.modules.jobs.jobs_service import (
    JobFilterQuery,
    JobListQuery,
    job_ordering,
    job_query,
)

NOW = datetime(2026, 10, 21, tzinfo=UTC)


def sql(clause: Any) -> str:
    return " ".join(str(clause.compile(dialect=postgresql.dialect())).split())


def test_status_filter_follows_job_to_out() -> None:
    """`job_to_out` hiện job `active` đã quá hạn nộp là `expired`: bộ lọc theo cùng định nghĩa."""
    assert status_service.status_filter("", NOW) is None
    assert status_service.status_filter("all", NOW) is None
    active = sql(status_service.status_filter("active", NOW))
    assert "jobs.status = " in active
    assert "jobs.expires_at IS NULL OR jobs.expires_at >= " in active
    expired = sql(status_service.status_filter("expired", NOW))
    assert expired.startswith("jobs.status = ")
    assert " OR jobs.status = " in expired
    assert "jobs.expires_at < " in expired
    assert "expires_at" not in sql(status_service.status_filter("removed", NOW))
    shown = sql(status_service.shown_status(NOW))
    assert shown.startswith("CASE WHEN (jobs.status = ")
    assert "jobs.expires_at < " in shown


def test_job_query_default_is_open_jobs() -> None:
    query = sql(job_query(JobFilterQuery(), now=NOW))
    assert "jobs.expires_at IS NULL OR jobs.expires_at >= " in query
    assert "WHERE" not in sql(job_query(JobFilterQuery(status="all"), now=NOW))


def test_salary_filter_and_sort_use_monthly_vnd() -> None:
    """Lọc và sắp cùng một biểu thức lương tháng VND (lương năm / 12), đúng chuỗi của index."""
    assert "salary_period = 'year' THEN coalesce(salary_max, salary_min) / 12" in (
        JOB_SALARY_MONTH_SQL
    )
    query = sql(job_query(JobFilterQuery(salary_from=30), now=NOW))
    assert f"{JOB_SALARY_MONTH_SQL} >= " in query
    assert "salary_currency =" not in query.replace(JOB_SALARY_MONTH_SQL, "")
    order = [sql(c) for c in job_ordering("salary", SortOrder.DESC)]
    assert order[0] == f"{JOB_SALARY_MONTH_SQL} DESC NULLS LAST"


@pytest.mark.parametrize("value", ["maybe", "WRONG", "1"])
def test_unknown_reviewed_filter_is_rejected(value: str) -> None:
    """Giá trị lạ của `reviewed` → 422, không bị hiểu là "sai"."""
    with pytest.raises(ValidationError):
        JobListQuery.model_validate({"reviewed": value})
    assert JobListQuery.model_validate({"reviewed": "wrong"}).reviewed == "wrong"
