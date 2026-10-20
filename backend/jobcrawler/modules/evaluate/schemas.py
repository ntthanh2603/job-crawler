"""Mô hình trả về của API đánh giá."""

from __future__ import annotations

from uuid import UUID

from jobcrawler.common.api import ApiOut


class EvalServerOut(ApiOut):
    available: bool
    path: str


class CompanyEvalOut(ApiOut):
    # Trường của `evaluate.CompanyEval` + precision / recall (tests/test_openapi.py kiểm khớp).
    name: str
    company_id: UUID | None
    golden: int
    predicted: int
    matched: int
    run_status: str | None
    missing: list[str]
    extra: list[str]
    precision: float | None
    recall: float | None


class SourceEvalOut(ApiOut):
    # Trường của `evaluate.SourceEval` + precision / recall (tests/test_openapi.py kiểm khớp).
    source: str
    predicted: int
    matched: int
    golden: int
    precision: float | None
    recall: float | None


class EvalReportOut(ApiOut):
    precision: float | None
    recall: float | None
    golden: int
    predicted: int
    matched: int
    target_precision: float
    target_recall: float
    perf: dict[str, str]  # tên chỉ số → giá trị đã định dạng
    companies: list[CompanyEvalOut]
    sources: list[SourceEvalOut]
    warnings: list[str]
    markdown: str
