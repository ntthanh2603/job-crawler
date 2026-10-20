from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from jobcrawler.common.schemas import CompanyIn, JobLevel, JobOut


def test_company_in_cleans_blanks() -> None:
    c = CompanyIn(name="  Công   ty  ABC ", tax_code=" ", website="", note=None)
    assert c.name == "Công ty ABC"
    assert c.tax_code is None
    assert c.website is None


def test_company_in_requires_name() -> None:
    with pytest.raises(ValidationError):
        CompanyIn(name="   ")


def _job(**overrides: object) -> dict[str, object]:
    now = datetime.now(UTC)
    data: dict[str, object] = {
        "title": "Backend Engineer",
        "title_normalized": "backend engineer",
        "company_id": "00000000-0000-7000-8000-000000000001",
        "company_name": "ABC",
        "description_md": "x" * 300,
        "canonical_source": "greenhouse",
        "confidence": 0.9,
        "first_seen_at": now,
        "last_seen_at": now,
    }
    data.update(overrides)
    return data


def test_job_out_defaults() -> None:
    job = JobOut.model_validate(_job())
    assert job.level is JobLevel.UNKNOWN
    assert job.status == "active"
    assert job.locations == []


@pytest.mark.parametrize("confidence", [-0.1, 1.1])
def test_job_out_confidence_range(confidence: float) -> None:
    with pytest.raises(ValidationError):
        JobOut.model_validate(_job(confidence=confidence))


def test_job_out_rejects_unknown_enum() -> None:
    with pytest.raises(ValidationError):
        JobOut.model_validate(_job(level="ceo"))
