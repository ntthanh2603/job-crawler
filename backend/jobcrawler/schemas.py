"""Pydantic DTO dùng chung cho mọi entrypoint (CLI, worker, sau này là API).

Tách riêng khỏi model SQLAlchemy ở `jobcrawler.db.models`.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Cho mô hình cũng là JSON trả ra API (`web/api.py`): trường có mặc định luôn có trong JSON nên spec
# OpenAPI ghi là bắt buộc. Chỉ đổi JSON schema chế độ serialization, không đổi cách kiểm dữ liệu.
API_OUTPUT = ConfigDict(json_schema_serialization_defaults_required=True)


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


class RunKind(StrEnum):
    """Loại lượt chạy một doanh nghiệp."""

    BUILD = "build"  # đủ: KYC, tìm nguồn, đọc hết danh sách + chi tiết, ghi bộ khung
    POLL = "poll"  # chỉ các trang/API trong bộ khung, tải chi tiết tin mới


class RunTrigger(StrEnum):
    """Vì sao lượt chạy được xếp."""

    NIGHTLY = "nightly"  # build ban đêm theo lịch
    SCHEDULE = "schedule"  # poll ban ngày theo chu kỳ
    ERROR = "error"  # poll lỗi → build lại bộ khung
    BOARD = "board"  # trang tuyển dụng có tin mới của doanh nghiệp
    NEW = "new"  # doanh nghiệp chưa có bộ khung
    MANUAL = "manual"  # người dùng / CLI
    CONTINUATION = "continuation"  # lượt build `partial` chạy tiếp


class PlanStatus(StrEnum):
    """Trạng thái bộ khung một nguồn (`crawl_plans`)."""

    ACTIVE = "active"
    BROKEN = "broken"  # poll lỗi: chờ build lại
    RETIRED = "retired"  # lượt build gần nhất không còn thấy nguồn này


class ScheduleState(StrEnum):
    """Trạng thái lịch thu thập của một doanh nghiệp (`company_schedules.state`)."""

    NEW = "new"  # chưa build lần nào
    READY = "ready"  # có bộ khung, đang poll theo chu kỳ
    NO_SOURCES = "no_sources"  # build xong nhưng không có nguồn nào cần poll (chỉ trang tuyển dụng)
    BROKEN = "broken"  # poll lỗi, đã xếp build lại
    PAUSED = "paused"  # tắt thu thập doanh nghiệp này


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


class ExtractionMethod(StrEnum):
    """Cách bóc một tin, theo thứ tự tin cậy giảm dần."""

    ATS_API = "ats_api"  # API JSON công khai của ATS
    JSONLD = "jsonld"  # schema.org/JobPosting nhúng trong trang
    PARSER = "parser"  # parser riêng của một site
    LLM = "llm"  # LLM bóc từ markdown của trang
    HEURISTIC = "heuristic"  # không có LLM: tiêu đề + nội dung chính của trang


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
    model_config = ConfigDict(
        from_attributes=True, json_schema_serialization_defaults_required=True
    )

    id: UUID
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


class KycEvidence(BaseModel):
    """Một bằng chứng KYC: URL + trích đoạn có trên trang đó."""

    url: str
    snippet: str
    # llm (trích đoạn LLM chọn, đã kiểm có trong ứng viên) | tax_code | site_name | domain_name
    # | panel | input
    kind: str


class KycCandidate(BaseModel):
    id: str  # W1 (website), R1 (tra cứu MST/đăng ký), C1 (nguồn khác)
    kind: Literal["website", "registry", "other"]
    domain: str
    url: str
    title: str | None = None
    snippet: str | None = None
    tax_codes: list[str] = Field(default_factory=list)
    rank: int | None = None  # thứ hạng trên SERP
    flags: list[str] = Field(default_factory=list)  # input | panel | fetched


class KycRecord(BaseModel):
    """Nội dung `companies.kyc_evidence`."""

    evidence: list[KycEvidence] = Field(default_factory=list)
    signals: list[str] = Field(default_factory=list)  # tín hiệu khớp mạnh / mâu thuẫn
    candidates: list[KycCandidate] = Field(default_factory=list)
    picked: str | None = None  # mã ứng viên website được chọn
    reason: str | None = None
    serp: str | None = None  # provider nếu đã dùng SERP; None = chưa tìm bằng SERP
    llm: str | None = None  # "<prompt>@<version>" nếu LLM đã chọn
    # Lần kiểm lại gần nhất không xác nhận được nhưng giữ kết quả `verified` cũ.
    last_check: dict[str, Any] | None = None


class RawJob(BaseModel):
    """Một tin bóc từ một nguồn, chưa chuẩn hoá (lưu vào `job_postings.raw`).

    Giữ nguyên giá trị nguồn trả về (HTML, chuỗi địa điểm, lương...); chuẩn hoá ở
    `pipeline/normalize.py`. Ngày chỉ được parse khi nguồn trả định dạng rõ ràng (ISO, epoch);
    dạng tương đối như "Posted 3 Days Ago" giữ ở `*_raw`.
    """

    source: str  # loại nguồn: greenhouse, lever, ..., careers
    external_id: str  # duy nhất trong `source`
    url: str
    title: str
    company_name: str | None = None  # tên nhà tuyển dụng nguồn ghi trên tin
    description: str | None = None
    description_format: Literal["html", "markdown", "text"] = "html"
    requirements: str | None = None  # cùng định dạng với description
    benefits: str | None = None
    locations: list[str] = Field(default_factory=list)
    work_mode: str | None = None  # giá trị thô: remote, hybrid, onsite, TELECOMMUTE...
    employment_type: str | None = None
    department: str | None = None
    level: str | None = None
    salary_raw: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_period: str | None = None
    experience_raw: str | None = None
    skills: list[str] = Field(default_factory=list)
    posted_at: datetime | None = None
    posted_raw: str | None = None
    expires_at: datetime | None = None
    expires_raw: str | None = None
    apply_url: str | None = None
    method: ExtractionMethod
    extra: dict[str, Any] = Field(default_factory=dict)


class Location(BaseModel):
    model_config = API_OUTPUT

    province: str | None = None  # một trong 34 tỉnh/thành sau sáp nhập 2025
    address_raw: str | None = None


class Salary(BaseModel):
    model_config = API_OUTPUT

    min: float | None = None
    max: float | None = None
    currency: str | None = None
    period: SalaryPeriod | None = None
    negotiable: bool = False
    raw: str | None = None


class ExperienceYears(BaseModel):
    model_config = API_OUTPUT

    min: float | None = None
    max: float | None = None


class JobOut(BaseModel):
    """Một job sau khi chuẩn hoá, xác minh và gộp trùng."""

    model_config = API_OUTPUT

    title: str
    title_normalized: str
    company_id: UUID
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
    company_id: UUID
    company_name: str
    status: RunStatus
    kyc_status: KycStatus | None = None
    website: str | None = None  # domain/URL đã dùng để tìm nguồn
    started_at: datetime
    finished_at: datetime
    duration_ms: int
    jobs_found: int = 0
    jobs_accepted: int = 0
    incomplete_sources: list[str] = Field(default_factory=list)
    error: str | None = None
    jobs: list[JobOut] = Field(default_factory=list)
