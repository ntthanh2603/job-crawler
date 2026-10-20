"""Hợp đồng API ↔ giao diện: spec commit ở backend/openapi.json khớp mã, mọi endpoint JSON có
mô hình trả về, mô hình chép từ dataclass của services không thiếu / thừa trường."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from jobcrawler.db import compress
from jobcrawler.services import batch, evaluate, sla, stats
from jobcrawler.web import actions, api
from jobcrawler.web.app import openapi_document

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
NOT_JSON = {"/api/failed-pages/{page_id}/raw", "/api/jobs/export"}


def test_every_json_endpoint_has_a_response_model(spec: dict) -> None:
    untyped = []
    for path, ops in spec["paths"].items():
        if path in NOT_JSON:
            continue
        for method, op in ops.items():
            schema = op["responses"]["200"]["content"]["application/json"]["schema"]
            if "$ref" not in schema and schema.get("type") != "array":
                untyped.append(f"{method.upper()} {path}")
    assert untyped == []


def test_response_fields_are_required(spec: dict) -> None:
    """Trường có mặc định vẫn luôn có trong JSON trả về: spec ghi bắt buộc (`ApiOut`, `API_OUTPUT`),
    kiểu TypeScript sinh ra không thành `x?:`. Chỉ mô hình nhận vào và lỗi của FastAPI có trường
    tuỳ chọn."""
    inputs = {"EnqueueIn", "ReviewIn", "SettingsUpdate", "Body_post_import", "Body_post_eval"}
    fastapi_errors = {"HTTPValidationError", "ValidationError"}
    optional = {
        name: sorted(set(schema.get("properties", {})) - set(schema.get("required", [])))
        for name, schema in spec["components"]["schemas"].items()
        if name not in inputs | fastapi_errors
    }
    assert {name: keys for name, keys in optional.items() if keys} == {}


# Mô hình trả về chép trường từ dataclass của services (+ property tính thêm).
MIRRORS: list[tuple[type[BaseModel], type, set[str]]] = [
    (api.QueueStatsOut, stats.QueueStats, set()),
    (api.RunStatsOut, stats.RunStats, set()),
    (api.SourceStatsOut, stats.SourceStats, set()),
    (api.BoardStatsOut, stats.BoardStats, set()),
    (api.TargetOut, stats.Target, set()),
    (api.StatsOut, stats.Stats, set()),
    (api.StorageStatsOut, compress.StorageStats, set()),
    (api.PollStatsOut, sla.PollStats, set()),
    (api.FreshnessOut, sla.Freshness, set()),
    (api.ImportOutcomeOut, actions.ImportOutcome, set()),
    (api.EnqueueResultOut, batch.EnqueueResult, set()),
    (api.CompanyEvalOut, evaluate.CompanyEval, {"precision", "recall"}),
    (api.SourceEvalOut, evaluate.SourceEval, {"precision", "recall"}),
    (api.SourceReviewOut, evaluate.SourceReview, {"precision"}),
]


@pytest.mark.parametrize(
    ("model", "source", "computed"), MIRRORS, ids=[m.__name__ for m, _, _ in MIRRORS]
)
def test_response_model_mirrors_dataclass(
    model: type[BaseModel], source: type, computed: set[str]
) -> None:
    fields = {f.name for f in dataclasses.fields(source)} | computed
    assert set(model.model_fields) == fields
