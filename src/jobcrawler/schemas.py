"""Pydantic DTO dùng chung cho mọi entrypoint (CLI, worker, sau này là API).

Tách riêng khỏi model SQLAlchemy ở `jobcrawler.db.models`.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class KycStatus(StrEnum):
    VERIFIED = "verified"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"


class RunStatus(StrEnum):
    RUNNING = "running"  # đang chạy, hoặc worker chết giữa chừng
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"
    AMBIGUOUS = "ambiguous"


class FetchOutcome(StrEnum):
    OK = "ok"
    BLOCKED = "blocked"
    TIMEOUT = "timeout"
    ERROR = "error"
    ROBOTS_DISALLOWED = "robots_disallowed"


class JobLevel(StrEnum):
    INTERN = "intern"
    FRESHER = "fresher"
    JUNIOR = "junior"
    MIDDLE = "middle"
    SENIOR = "senior"
    LEAD = "lead"
    MANAGER = "manager"
    DIRECTOR = "director"
    UNKNOWN = "unknown"


class EmploymentType(StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    INTERNSHIP = "internship"
    FREELANCE = "freelance"
    UNKNOWN = "unknown"


class WorkMode(StrEnum):
    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE = "remote"
    UNKNOWN = "unknown"


class JobStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    REMOVED = "removed"


class SalaryPeriod(StrEnum):
    MONTH = "month"
    YEAR = "year"
    HOUR = "hour"


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


class CompanyIn(BaseModel):
    """Một dòng của `companies.csv`."""

    name: str = Field(min_length=1)
    tax_code: str | None = None
    website: str | None = None
    note: str | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _strip_name(cls, value: Any) -> Any:
        return " ".join(value.split()) if isinstance(value, str) else value

    @field_validator("tax_code", "website", "note", mode="before")
    @classmethod
    def _optional(cls, value: Any) -> Any:
        return _blank_to_none(value)


class CompanyEntity(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    input_name: str
    legal_name: str | None = None
    brand_name: str | None = None
    tax_code: str | None = None
    primary_domain: str | None = None
    other_domains: list[str] = Field(default_factory=list)
    address: str | None = None
    kyc_status: KycStatus | None = None
    kyc_confidence: float | None = None
    kyc_evidence: Any = None
    kyc_at: datetime | None = None

    @property
    def display_name(self) -> str:
        return self.brand_name or self.legal_name or self.input_name


class Location(BaseModel):
    province: str | None = None  # một trong 34 tỉnh/thành sau sáp nhập 2025
    address_raw: str | None = None


class Salary(BaseModel):
    min: float | None = None
    max: float | None = None
    currency: str | None = None
    period: SalaryPeriod | None = None
    negotiable: bool = False
    raw: str | None = None


class ExperienceYears(BaseModel):
    min: float | None = None
    max: float | None = None


class JobOut(BaseModel):
    """Một job sau khi chuẩn hoá, xác minh và gộp trùng."""

    title: str
    title_normalized: str
    company_id: int
    company_name: str
    level: JobLevel = JobLevel.UNKNOWN
    employment_type: EmploymentType = EmploymentType.UNKNOWN
    work_mode: WorkMode = WorkMode.UNKNOWN
    locations: list[Location] = Field(default_factory=list)
    salary: Salary | None = None
    experience_years: ExperienceYears | None = None
    description_md: str
    requirements_md: str | None = None
    benefits_md: str | None = None
    skills: list[str] = Field(default_factory=list)
    posted_at: datetime | None = None
    expires_at: datetime | None = None
    apply_url: str | None = None
    source_urls: list[str] = Field(default_factory=list)
    canonical_source: str
    status: JobStatus = JobStatus.ACTIVE
    confidence: float = Field(ge=0, le=1)
    verification_reasons: list[str] = Field(default_factory=list)
    first_seen_at: datetime
    last_seen_at: datetime


class CompanyResult(BaseModel):
    """Kết quả một lần chạy `process_company`."""

    run_id: UUID
    company_id: int
    company_name: str
    status: RunStatus
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    jobs_found: int = 0
    jobs_accepted: int = 0
    incomplete_sources: list[str] = Field(default_factory=list)
    error: str | None = None
    jobs: list[JobOut] = Field(default_factory=list)
