"""Model SQLAlchemy. Schema thật được tạo bằng Alembic (`jobcrawler db upgrade`)."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    LargeBinary,
    MetaData,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from jobcrawler.schemas import (
    EmploymentType,
    FetchOutcome,
    JobLevel,
    JobStatus,
    KycStatus,
    RunStatus,
    SalaryPeriod,
    WorkMode,
)

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

EMPTY_ARRAY = text("'{}'::text[]")
EMPTY_OBJECT = text("'{}'::jsonb")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        str: Text,
        datetime: DateTime(timezone=True),
        float: Float,
        list[str]: ARRAY(Text),
        dict[str, Any]: JSONB,
        uuid.UUID: UUID(as_uuid=True),
    }


def one_of(column: str, values: type[StrEnum], name: str | None = None) -> CheckConstraint:
    allowed = ", ".join(f"'{v.value}'" for v in values)
    return CheckConstraint(f"{column} IN ({allowed})", name=name or column)


def _pk() -> Mapped[int]:
    return mapped_column(BigInteger, Identity(), primary_key=True)


def _created_at() -> Mapped[datetime]:
    return mapped_column(server_default=func.now())


def _updated_at() -> Mapped[datetime]:
    return mapped_column(server_default=func.now(), onupdate=func.now())


class Company(Base):
    __tablename__ = "companies"
    __table_args__ = (one_of("kyc_status", KycStatus),)

    id: Mapped[int] = _pk()
    input_name: Mapped[str]
    legal_name: Mapped[str | None]
    brand_name: Mapped[str | None]
    tax_code: Mapped[str | None] = mapped_column(unique=True)
    primary_domain: Mapped[str | None] = mapped_column(index=True)
    other_domains: Mapped[list[str]] = mapped_column(server_default=EMPTY_ARRAY)
    address: Mapped[str | None]
    kyc_status: Mapped[str | None]
    kyc_confidence: Mapped[float | None]
    # Danh sách bằng chứng [{url, snippet}] (hoặc ứng viên khi `ambiguous`).
    kyc_evidence: Mapped[Any | None] = mapped_column(JSONB)
    kyc_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class CompanyAlias(Base):
    __tablename__ = "company_aliases"
    __table_args__ = (
        UniqueConstraint("company_id", "alias_norm"),
        Index("ix_company_aliases_alias_norm", "alias_norm"),
        Index(
            "ix_company_aliases_alias_norm_trgm",
            "alias_norm",
            postgresql_using="gin",
            postgresql_ops={"alias_norm": "gin_trgm_ops"},
        ),
    )

    id: Mapped[int] = _pk()
    company_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    alias: Mapped[str]
    alias_norm: Mapped[str]
    # input | legal | short | brand | english
    kind: Mapped[str | None]
    created_at: Mapped[datetime] = _created_at()


class SourceProfile(Base):
    """Profile nhà tuyển dụng trên một trang tuyển dụng, map về `companies`."""

    __tablename__ = "source_profiles"
    __table_args__ = (
        UniqueConstraint("source", "external_id"),
        Index(
            "ix_source_profiles_name_norm_trgm",
            "name_norm",
            postgresql_using="gin",
            postgresql_ops={"name_norm": "gin_trgm_ops"},
        ),
    )

    id: Mapped[int] = _pk()
    source: Mapped[str]
    external_id: Mapped[str]
    url: Mapped[str]
    name_on_source: Mapped[str]
    # Tên đã chuẩn hoá như `company_aliases.alias_norm`, để so trigram khi map.
    name_norm: Mapped[str | None]
    website: Mapped[str | None]  # website doanh nghiệp ghi trên trang tuyển dụng
    tax_code: Mapped[str | None]
    # Thông tin khác trên trang (logo, địa chỉ, quy mô...).
    info: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_OBJECT)
    company_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("companies.id", ondelete="SET NULL"), index=True
    )
    match_confidence: Mapped[float | None]
    match_evidence: Mapped[Any | None] = mapped_column(JSONB)
    # Lần cuối đã thử map (kể cả không map được) — để chỉ map lại khi có doanh nghiệp mới.
    matched_at: Mapped[datetime | None]
    # Lần cuối mọi tin của profile này được làm mới (bởi lượt index hoặc crawl trực tiếp).
    indexed_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class JobPosting(Base):
    """Một tin trên một nguồn, trước khi gộp."""

    __tablename__ = "job_postings"
    __table_args__ = (
        UniqueConstraint("source", "external_id"),
        one_of("status", JobStatus),
    )

    id: Mapped[int] = _pk()
    source: Mapped[str]
    external_id: Mapped[str]
    url: Mapped[str]
    source_profile_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("source_profiles.id", ondelete="SET NULL"), index=True
    )
    # use_alter: jobs.canonical_posting_id ↔ job_postings.job_id tham chiếu vòng.
    job_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("jobs.id", ondelete="SET NULL", use_alter=True), index=True
    )
    raw: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_OBJECT)
    content_hash: Mapped[str | None]
    first_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    status: Mapped[str] = mapped_column(server_default=JobStatus.ACTIVE.value)
    # Crawl tăng dần: `lastmod` trong sitemap, ETag/Last-Modified của trang chi tiết.
    lastmod: Mapped[datetime | None]
    etag: Mapped[str | None]
    http_last_modified: Mapped[str | None]
    fetched_at: Mapped[datetime | None]  # lần cuối tải trang chi tiết
    expires_at: Mapped[datetime | None]  # hạn nộp (validThrough) nếu nguồn ghi
    # Số lượt index liên tiếp không còn thấy tin trên nguồn (≥ 2 → removed).
    missed_runs: Mapped[int] = mapped_column(server_default=text("0"))
    # Kết quả xác minh lần gần nhất (`pipeline/verify.py`), ghi cho cả tin bị loại.
    company_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("companies.id", ondelete="SET NULL"), index=True
    )
    accepted: Mapped[bool | None]
    confidence: Mapped[float | None]
    verification_reasons: Mapped[list[str]] = mapped_column(server_default=EMPTY_ARRAY)
    verified_at: Mapped[datetime | None]


class BoardIndexRun(Base):
    """Một lượt index tăng dần một trang tuyển dụng (`jobcrawler index-boards`, cron)."""

    __tablename__ = "board_index_runs"
    __table_args__ = (
        one_of("status", RunStatus),
        Index("ix_board_index_runs_source_started_at", "source", "started_at"),
    )

    id: Mapped[int] = _pk()
    source: Mapped[str]
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(server_default=RunStatus.RUNNING.value)
    # Danh sách URL tin đọc đủ (sitemap/listing không lỗi, không bị cắt) — mới được đánh dấu
    # tin không còn thấy.
    complete: Mapped[bool] = mapped_column(server_default=text("false"))
    urls_seen: Mapped[int] = mapped_column(server_default=text("0"))
    fetched: Mapped[int] = mapped_column(server_default=text("0"))
    new: Mapped[int] = mapped_column(server_default=text("0"))
    updated: Mapped[int] = mapped_column(server_default=text("0"))
    unchanged: Mapped[int] = mapped_column(server_default=text("0"))
    failed: Mapped[int] = mapped_column(server_default=text("0"))
    removed: Mapped[int] = mapped_column(server_default=text("0"))
    profiles_new: Mapped[int] = mapped_column(server_default=text("0"))
    profiles_matched: Mapped[int] = mapped_column(server_default=text("0"))
    error: Mapped[str | None]
    stats: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_OBJECT)


class Job(Base):
    """Job sau khi gộp trùng; các cột tương ứng `schemas.JobOut`."""

    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("company_id", "cluster_key"),
        one_of("level", JobLevel),
        one_of("employment_type", EmploymentType),
        one_of("work_mode", WorkMode),
        one_of("status", JobStatus),
        one_of("salary_period", SalaryPeriod),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
    )

    id: Mapped[int] = _pk()
    company_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    cluster_key: Mapped[str]
    canonical_posting_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("job_postings.id", ondelete="SET NULL")
    )
    title: Mapped[str]
    title_normalized: Mapped[str]
    company_name: Mapped[str]
    level: Mapped[str] = mapped_column(server_default=JobLevel.UNKNOWN.value)
    employment_type: Mapped[str] = mapped_column(server_default=EmploymentType.UNKNOWN.value)
    work_mode: Mapped[str] = mapped_column(server_default=WorkMode.UNKNOWN.value)
    # [{province, address_raw}]
    locations: Mapped[Any] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    salary_min: Mapped[float | None]
    salary_max: Mapped[float | None]
    salary_currency: Mapped[str | None]
    salary_period: Mapped[str | None]
    salary_negotiable: Mapped[bool] = mapped_column(server_default=text("false"))
    salary_raw: Mapped[str | None]
    experience_years_min: Mapped[float | None]
    experience_years_max: Mapped[float | None]
    description_md: Mapped[str]
    requirements_md: Mapped[str | None]
    benefits_md: Mapped[str | None]
    skills: Mapped[list[str]] = mapped_column(server_default=EMPTY_ARRAY)
    posted_at: Mapped[datetime | None]
    expires_at: Mapped[datetime | None]
    apply_url: Mapped[str | None]
    source_urls: Mapped[list[str]] = mapped_column(server_default=EMPTY_ARRAY)
    canonical_source: Mapped[str]
    status: Mapped[str] = mapped_column(server_default=JobStatus.ACTIVE.value)
    confidence: Mapped[float]
    verification_reasons: Mapped[list[str]] = mapped_column(server_default=EMPTY_ARRAY)
    first_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class CompanyRun(Base):
    __tablename__ = "company_runs"
    __table_args__ = (
        one_of("status", RunStatus),
        Index("ix_company_runs_company_id_started_at", "company_id", "started_at"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("companies.id", ondelete="CASCADE")
    )
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    duration_ms: Mapped[int | None]
    status: Mapped[str] = mapped_column(server_default=RunStatus.RUNNING.value)
    jobs_found: Mapped[int] = mapped_column(server_default=text("0"))
    jobs_accepted: Mapped[int] = mapped_column(server_default=text("0"))
    incomplete_sources: Mapped[list[str]] = mapped_column(server_default=EMPTY_ARRAY)
    error: Mapped[str | None]


class SourceFetch(Base):
    """Mỗi lần gọi một nguồn. `run_id`/`company_id` để trống với lượt index trang tuyển dụng."""

    __tablename__ = "source_fetches"
    __table_args__ = (one_of("outcome", FetchOutcome),)

    id: Mapped[int] = _pk()
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("company_runs.run_id", ondelete="CASCADE"), index=True
    )
    company_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("companies.id", ondelete="CASCADE")
    )
    source: Mapped[str]
    url: Mapped[str]
    http_status: Mapped[int | None]
    proxy_id: Mapped[str | None]
    duration_ms: Mapped[int | None]
    items: Mapped[int | None]
    outcome: Mapped[str]
    error: Mapped[str | None]
    fetched_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Log(Base):
    """Ghi theo lô bằng COPY từ `jobcrawler.logging.PgLogSink`; cột khớp `LOG_COLUMNS`."""

    __tablename__ = "logs"
    __table_args__ = (
        Index("ix_logs_ts", "ts", postgresql_using="brin"),
        Index("ix_logs_run_id", "run_id", postgresql_where=text("run_id IS NOT NULL")),
    )

    id: Mapped[int] = _pk()
    ts: Mapped[datetime] = mapped_column(server_default=func.now())
    level: Mapped[str]
    event: Mapped[str]
    run_id: Mapped[uuid.UUID | None]
    company_id: Mapped[int | None] = mapped_column(BigInteger)
    context: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_OBJECT)


class FailedPage(Base):
    """HTML (nén zstd) của trang parse lỗi, để debug. Xoá sau 7 ngày."""

    __tablename__ = "failed_pages"

    id: Mapped[int] = _pk()
    url: Mapped[str]
    source: Mapped[str]
    reason: Mapped[str]
    html_zstd: Mapped[bytes | None] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)
