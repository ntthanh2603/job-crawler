"""Hợp đồng API ↔ giao diện: spec commit ở backend/openapi.json khớp mã, mọi endpoint JSON có
mô hình trả về, mô hình chép từ dataclass của services không thiếu / thừa trường."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from jobcrawler.bootstrap.app import openapi_document
from jobcrawler.database import compress
from jobcrawler.modules.companies import batch
from jobcrawler.modules.companies.actions import CreateOutcome, ImportOutcome
from jobcrawler.modules.companies.schemas import (
    CompanyCreatedOut,
    EnqueueResultOut,
    ImportOutcomeOut,
)
from jobcrawler.modules.crawl import sla
from jobcrawler.modules.crawl.schemas import FreshnessOut, PollStatsOut
from jobcrawler.modules.evaluate import evaluate
from jobcrawler.modules.evaluate.schemas import CompanyEvalOut, SourceEvalOut
from jobcrawler.modules.jobs.schemas import SourceReviewOut
from jobcrawler.modules.overview import stats
from jobcrawler.modules.overview.schemas import (
    BoardStatsOut,
    QueueStatsOut,
    RunStatsOut,
    SourceStatsOut,
    StatsOut,
    StorageStatsOut,
    TargetOut,
)
from jobcrawler.modules.proxies import proxies
from jobcrawler.modules.proxies.schemas import ProxyCheckOut, ProxyStatusOut
from jobcrawler.services import health

SPEC = Path(__file__).resolve().parents[1] / "openapi.json"


@pytest.fixture(scope="module")
def spec() -> dict:
    return json.loads(openapi_document())


def test_committed_spec_is_up_to_date() -> None:
    assert SPEC.read_text(encoding="utf-8") == openapi_document(), (
        "backend/openapi.json đã cũ so với mã: chạy `task api` (backend/) rồi sinh lại kiểu của"
        " giao diện"
    )


def test_operation_ids_are_handler_names(spec: dict) -> None:
    ids = [op["operationId"] for ops in spec["paths"].values() for op in ops.values()]
    assert len(ids) == len(set(ids)), "trùng tên hàm xử lý giữa các router"
    assert spec["paths"]["/api/companies/{company_id}/jobs"]["get"]["operationId"] == (
        "get_company_jobs"
    )


# Endpoint không trả JSON (file tải về).
NOT_JSON = {
    "/api/failed-pages/{page_id}/raw",
    "/api/jobs/export",
    "/api/companies/import/sample.csv",
}


# Luồng Server-Sent Events: `data` của mỗi sự kiện là JSON theo mô hình này.
STREAMS = {"/api/events": "LiveOut"}


def test_every_json_endpoint_has_a_response_model(spec: dict) -> None:
    untyped = []
    for path, ops in spec["paths"].items():
        if path in NOT_JSON or path in STREAMS:
            continue
        for method, op in ops.items():
            schema = op["responses"]["200"]["content"]["application/json"]["schema"]
            if "$ref" not in schema and schema.get("type") != "array":
                untyped.append(f"{method.upper()} {path}")
    assert untyped == []


@pytest.mark.parametrize(("path", "model"), STREAMS.items())
def test_event_streams_have_a_data_model(spec: dict, path: str, model: str) -> None:
    content = spec["paths"][path]["get"]["responses"]["200"]["content"]
    data = content["text/event-stream"]["itemSchema"]["properties"]["data"]
    assert data["contentSchema"] == {"$ref": f"#/components/schemas/{model}"}


def test_response_fields_are_required(spec: dict) -> None:
    """Trường có mặc định vẫn luôn có trong JSON trả về: spec ghi bắt buộc (`ApiOut`, `API_OUTPUT`),
    kiểu TypeScript sinh ra không thành `x?:`. Chỉ mô hình nhận vào và lỗi của FastAPI có trường
    tuỳ chọn."""
    inputs = {
        "EnqueueIn",
        "ReviewIn",
        "CompanyCreateIn",
        "SettingsUpdate",
        "Body_post_import",
        "Body_post_eval",
    }
    fastapi_errors = {"HTTPValidationError", "ValidationError"}
    optional = {
        name: sorted(set(schema.get("properties", {})) - set(schema.get("required", [])))
        for name, schema in spec["components"]["schemas"].items()
        if name not in inputs | fastapi_errors
    }
    assert {name: keys for name, keys in optional.items() if keys} == {}


# Mô hình trả về chép trường từ dataclass của services (+ property tính thêm).
MIRRORS: list[tuple[type[BaseModel], type, set[str]]] = [
    (QueueStatsOut, stats.QueueStats, set()),
    (RunStatsOut, stats.RunStats, set()),
    (SourceStatsOut, stats.SourceStats, set()),
    (BoardStatsOut, stats.BoardStats, set()),
    (TargetOut, stats.Target, set()),
    (StatsOut, stats.Stats, set()),
    (StorageStatsOut, compress.StorageStats, set()),
    (PollStatsOut, sla.PollStats, set()),
    (FreshnessOut, sla.Freshness, set()),
    (ImportOutcomeOut, ImportOutcome, set()),
    (CompanyCreatedOut, CreateOutcome, set()),
    (EnqueueResultOut, batch.EnqueueResult, set()),
    (CompanyEvalOut, evaluate.CompanyEval, {"precision", "recall"}),
    (SourceEvalOut, evaluate.SourceEval, {"precision", "recall"}),
    (SourceReviewOut, evaluate.SourceReview, {"precision"}),
    (ProxyStatusOut, proxies.ProxyStatus, set()),
    (ProxyCheckOut, health.ProxyCheck, set()),
]


@pytest.mark.parametrize(
    ("model", "source", "computed"), MIRRORS, ids=[m.__name__ for m, _, _ in MIRRORS]
)
def test_response_model_mirrors_dataclass(
    model: type[BaseModel], source: type, computed: set[str]
) -> None:
    fields = {f.name for f in dataclasses.fields(source)} | computed
    assert set(model.model_fields) == fields
