from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jobcrawler.db.models import Job
from jobcrawler.pipeline.store import job_to_out, job_values
from jobcrawler.schemas import JobOut, JobStatus
from jobcrawler.services.export import CSV_COLUMNS, JobWriter, output_path

from .jobs_fixtures import NOW, normalized, raw_job


def job_row(**overrides: object) -> Job:
    values = job_values(
        normalized(raw_job()),
        company_id=7,
        company_name="Công ty Cổ phần ACME",
        source_urls=["https://careers.acme.vn/jobs/ke-toan-tong-hop", "https://topcv.vn/x"],
        confidence=0.92,
        reasons=["SOURCE_CAREERS", "EMPLOYER_MATCH"],
    )
    values |= {
        "id": 1,
        "cluster_key": "k",
        "first_seen_at": NOW - timedelta(days=3),
        "last_seen_at": NOW,
    }
    values |= overrides
    return Job(**values)


def test_job_to_out_round_trip() -> None:
    out = job_to_out(job_row())
    assert out.title == "Kế toán tổng hợp"
    assert out.company_id == 7
    assert out.locations[0].province == "Hà Nội"
    assert out.salary is not None
    assert (out.salary.min, out.salary.max, out.salary.currency) == (15e6, 20e6, "VND")
    assert out.requirements_md
    assert "2 năm" in out.requirements_md
    assert out.experience_years is not None
    assert out.experience_years.min == 2
    assert out.status is JobStatus.ACTIVE
    assert out.apply_url == "https://careers.acme.vn/jobs/ke-toan-tong-hop"
    # Quá hạn nộp khi export → expired.
    late = job_to_out(job_row(), now=datetime(2027, 1, 1, tzinfo=UTC))
    assert late.status is JobStatus.EXPIRED


def test_jsonl_lines_are_jobout() -> None:
    buf = io.StringIO()
    writer = JobWriter(buf, "jsonl")
    assert writer.write_all([job_to_out(job_row()), job_to_out(job_row(title="Kế toán kho"))]) == 2
    lines = buf.getvalue().splitlines()
    assert len(lines) == 2
    first = JobOut.model_validate_json(lines[0])
    assert first == job_to_out(job_row())
    assert "Kế toán tổng hợp" in lines[0]  # không escape tiếng Việt
    assert set(json.loads(lines[1])) == set(JobOut.model_fields)


def test_csv_has_header_once_and_flat_fields() -> None:
    buf = io.StringIO()
    writer = JobWriter(buf, "csv")
    writer.write_all([job_to_out(job_row())])
    writer.write_all([job_to_out(job_row(title="Kế toán kho"))])
    rows = list(csv.DictReader(io.StringIO(buf.getvalue())))
    assert len(rows) == 2
    assert tuple(rows[0]) == CSV_COLUMNS
    assert rows[0]["provinces"] == "Hà Nội"
    assert rows[0]["salary_min"] == "15000000"
    assert (
        rows[0]["source_urls"]
        == "https://careers.acme.vn/jobs/ke-toan-tong-hop | https://topcv.vn/x"
    )
    assert rows[0]["verification_reasons"] == "SOURCE_CAREERS | EMPLOYER_MATCH"
    assert rows[1]["title"] == "Kế toán kho"


def test_output_path() -> None:
    assert output_path("out/", "jsonl") == Path("out/jobs.jsonl")
    assert output_path("out", "csv") == Path("out/jobs.csv")
    assert output_path("exports/acme.jsonl", "jsonl") == Path("exports/acme.jsonl")
