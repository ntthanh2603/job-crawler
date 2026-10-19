"""Model SQLAlchemy. Schema thật được tạo bằng Alembic (`jobcrawler db upgrade`)."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    Float,
    ForeignKey,
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

from jobcrawler.db.compress import Kind, ZstdJSON, ZstdText
from jobcrawler.db.ids import uuid7
from jobcrawler.schemas import (
    EmploymentType,
    FetchOutcome,
    JobLevel,
    JobStatus,
    KycStatus,
    PlanStatus,
    RunKind,
    RunStatus,
    RunTrigger,
    SalaryPeriod,
    ScheduleState,
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
COMPRESSED = "zstd (db/compress.py): 1 byte phiên bản + 4 byte id từ điển + frame zstd"
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


def _pk() -> Mapped[uuid.UUID]:
    # UUIDv7 (db/ids.py); hàm SQL `uuid_v7()` (migration) cho insert bằng SQL thô / COPY.
    return mapped_column(primary_key=True, default=uuid7, server_default=text("uuid_v7()"))


def _created_at() -> Mapped[datetime]:
    return mapped_column(server_default=func.now())


def _updated_at() -> Mapped[datetime]:
    return mapped_column(server_default=func.now(), onupdate=func.now())


# Chuỗi tìm kiếm doanh nghiệp (cột sinh + GIN trigram): `||` thay concat_ws vì cột sinh cần hàm
# IMMUTABLE.
COMPANY_SEARCH_TEXT = (
    "coalesce(input_name, '') || ' ' || coalesce(legal_name, '') || ' ' || "
    "coalesce(brand_name, '') || ' ' || coalesce(tax_code, '') || ' ' || "
    "coalesce(primary_domain, '')"
)


class Company(Base):
    __tablename__ = "companies"
    __table_args__ = (
        one_of("kyc_status", KycStatus),
        Index(
            "ix_companies_search_text",
            "search_text",
            postgresql_using="gin",
            postgresql_ops={"search_text": "gin_trgm_ops"},
        ),
        Index("ix_companies_last_run_status", "last_run_status"),
        Index("ix_companies_kyc_status", "kyc_status"),
    )

    id: Mapped[uuid.UUID] = _pk()
    input_name: Mapped[str]
    # Website và ghi chú từ `companies.csv` (`jobcrawler import`), dùng lại khi enqueue.
    input_website: Mapped[str | None]
    note: Mapped[str | None]
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
    # Ghi sẵn để danh sách doanh nghiệp lọc/hiện không cần truy vấn con theo từng dòng: lượt chạy
    # mới nhất (cập nhật khi lượt bắt đầu / kết thúc) và số job đang mở (sau mỗi lần lưu job;
    # bảo trì định kỳ đối chiếu lại).
    active_jobs: Mapped[int] = mapped_column(server_default=text("0"))
    last_run_id: Mapped[uuid.UUID | None]
    last_run_status: Mapped[str | None]
    last_run_at: Mapped[datetime | None]
    last_run_duration_ms: Mapped[int | None]
    last_run_incomplete: Mapped[list[str]] = mapped_column(server_default=EMPTY_ARRAY)
    search_text: Mapped[str | None] = mapped_column(Computed(COMPANY_SEARCH_TEXT, persisted=True))
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

    id: Mapped[uuid.UUID] = _pk()
    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
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

    id: Mapped[uuid.UUID] = _pk()
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
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL"), index=True
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
        Index(
            "ix_job_postings_company_rejected",
            "company_id",
            text("verified_at DESC"),
            postgresql_where=text("accepted IS false"),
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    source: Mapped[str]
    external_id: Mapped[str]
    url: Mapped[str]
    source_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("source_profiles.id", ondelete="SET NULL"), index=True
    )
    # use_alter: jobs.canonical_posting_id ↔ job_postings.job_id tham chiếu vòng.
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL", use_alter=True), index=True
    )
    # RawJob dạng JSON, nén zstd (xem db/compress.py).
    raw: Mapped[dict[str, Any]] = mapped_column(
        ZstdJSON(Kind.POSTING), default=dict, comment=COMPRESSED
    )
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
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL"), index=True
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

    id: Mapped[uuid.UUID] = _pk()
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
        # Hai job y hệt (cùng dấu vân tay) không thể cùng đang mở ở một doanh nghiệp.
        Index(
            "ux_jobs_company_fingerprint_active",
            "company_id",
            "fingerprint",
            unique=True,
            postgresql_where=text("status = 'active' AND fingerprint IS NOT NULL"),
        ),
        # Danh sách việc làm: lọc trạng thái + từng kiểu sắp xếp (queries.SORTS), nguồn, tỉnh,
        # tìm kiếm; BRIN cho biểu đồ theo ngày thấy.
        Index(
            "ix_jobs_status_first_seen_at", "status", text("first_seen_at DESC"), text("id DESC")
        ),
        Index(
            "ix_jobs_status_posted_at", "status", text("posted_at DESC NULLS LAST"), text("id DESC")
        ),
        Index("ix_jobs_status_expires_at", "status", "expires_at", text("id DESC")),
        Index(
            "ix_jobs_status_salary",
            "status",
            text("coalesce(salary_max, salary_min) DESC NULLS LAST"),
            text("id DESC"),
        ),
        Index("ix_jobs_status_source_kind", "status", text("split_part(canonical_source, ':', 1)")),
        Index("ix_jobs_company_id_status", "company_id", "status", text("first_seen_at DESC")),
        Index(
            "ix_jobs_locations",
            "locations",
            postgresql_using="gin",
            postgresql_ops={"locations": "jsonb_path_ops"},
        ),
        Index(
            "ix_jobs_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        ),
        Index(
            "ix_jobs_company_name_trgm",
            "company_name",
            postgresql_using="gin",
            postgresql_ops={"company_name": "gin_trgm_ops"},
        ),
        Index("ix_jobs_first_seen_at_brin", "first_seen_at", postgresql_using="brin"),
    )

    id: Mapped[uuid.UUID] = _pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    cluster_key: Mapped[str]
    # Dấu vân tay nội dung (pipeline/dedup.fingerprint): tiêu đề + nơi làm + JD đã chuẩn hoá.
    fingerprint: Mapped[str | None]
    canonical_posting_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("job_postings.id", ondelete="SET NULL")
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
    # Ba phần nội dung dài nén zstd (xem db/compress.py); đọc qua ORM như chuỗi thường.
    description_md: Mapped[str] = mapped_column(ZstdText(Kind.JOB_TEXT), comment=COMPRESSED)
    requirements_md: Mapped[str | None] = mapped_column(ZstdText(Kind.JOB_TEXT), comment=COMPRESSED)
    benefits_md: Mapped[str | None] = mapped_column(ZstdText(Kind.JOB_TEXT), comment=COMPRESSED)
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


class JobReview(Base):
    """Người kiểm đánh dấu một job đúng/sai (precision khi chưa có golden set, trang Kiểm tay)."""

    __tablename__ = "job_reviews"

    id: Mapped[uuid.UUID] = _pk()
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), unique=True
    )
    correct: Mapped[bool]
    note: Mapped[str | None]
    reviewer: Mapped[str | None]
    reviewed_at: Mapped[datetime] = mapped_column(server_default=func.now())


class CompanyRun(Base):
    """Một lượt chạy một doanh nghiệp: `build` (đủ, ghi bộ khung) hoặc `poll` (theo bộ khung).

    Lượt poll không đổi gì (không tin mới, không tin đóng, không lỗi) không ghi dòng ở đây, chỉ
    cập nhật `company_schedules` — ban ngày mỗi doanh nghiệp poll vài trăm lượt."""

    __tablename__ = "company_runs"
    __table_args__ = (
        one_of("status", RunStatus),
        one_of("kind", RunKind),
        one_of("trigger", RunTrigger),
        Index("ix_company_runs_company_id_started_at", "company_id", "started_at"),
        Index("ix_company_runs_started_at", "started_at"),
        Index("ix_company_runs_kind_started_at", "kind", "started_at"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(server_default=RunKind.BUILD.value)
    trigger: Mapped[str] = mapped_column(server_default=RunTrigger.MANUAL.value)
    # Lúc job sẵn sàng chạy (xếp hàng / tới giờ hẹn); `started_at - queued_at` = thời gian chờ.
    queued_at: Mapped[datetime | None]
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    duration_ms: Mapped[int | None]
    status: Mapped[str] = mapped_column(server_default=RunStatus.RUNNING.value)
    jobs_found: Mapped[int] = mapped_column(server_default=text("0"))
    jobs_accepted: Mapped[int] = mapped_column(server_default=text("0"))
    # Tin mới thấy / việc bị đóng ở lượt này (so với lượt trước).
    jobs_new: Mapped[int] = mapped_column(server_default=text("0"))
    jobs_closed: Mapped[int] = mapped_column(server_default=text("0"))
    incomplete_sources: Mapped[list[str]] = mapped_column(server_default=EMPTY_ARRAY)
    error: Mapped[str | None]


class CrawlPlan(Base):
    """Bộ khung thu thập một nguồn (trang careers / bảng ATS) của doanh nghiệp.

    Lượt build ghi lại đúng những gì cần để lấy lại danh sách tin: trang danh sách (kể cả trang
    sau, trang danh mục dẫn tới tin), có phải mở bằng trình duyệt không, API danh sách tin của
    trang SPA (cách phân trang), link đã mở mà không phải tin. Lượt poll chỉ đi theo `spec`, không
    tìm nguồn lại; nguồn không lấy được nữa thì `broken` và doanh nghiệp được build lại.
    """

    __tablename__ = "crawl_plans"
    __table_args__ = (
        UniqueConstraint("company_id", "source"),
        one_of("status", PlanStatus),
    )

    id: Mapped[uuid.UUID] = _pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    source: Mapped[str]  # tên nguồn, vd. `careers:tuyendung.vng.com.vn`, `greenhouse:gitlab`
    kind: Mapped[str]  # careers | greenhouse | lever | ...
    url: Mapped[str]  # trang / API bắt đầu
    # Bộ khung (sources/plan.py: PlanSpec): listing, render, apis, hubs, ignored, jobs...
    spec: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_OBJECT)
    status: Mapped[str] = mapped_column(server_default=PlanStatus.ACTIVE.value)
    jobs: Mapped[int] = mapped_column(server_default=text("0"))  # số tin lượt gần nhất
    # Mã tin (`external_id`) nguồn trả về ở lượt đủ gần nhất: lượt poll so tập này để biết tin
    # mới / tin đã gỡ mà không phải đọc nội dung tin đã lưu.
    job_keys: Mapped[list[str]] = mapped_column(server_default=EMPTY_ARRAY)
    signature: Mapped[str | None]  # hash của `job_keys`
    built_run_id: Mapped[uuid.UUID | None]
    built_at: Mapped[datetime]
    polled_at: Mapped[datetime | None]
    changed_at: Mapped[datetime | None]
    failures: Mapped[int] = mapped_column(server_default=text("0"))  # lỗi poll liên tiếp
    last_error: Mapped[str | None]
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class CompanySchedule(Base):
    """Lịch thu thập của một doanh nghiệp: build đêm, poll ngày. Cập nhật mỗi lượt poll (vài chục
    dòng/giây ở 10.000 doanh nghiệp) nên tách khỏi `companies` và để fillfactor thấp."""

    __tablename__ = "company_schedules"
    __table_args__ = (
        one_of("state", ScheduleState),
        Index(
            "ix_company_schedules_next_poll_at",
            "next_poll_at",
            postgresql_where=text("state = 'ready'"),
        ),
        Index("ix_company_schedules_next_build_at", "next_build_at"),
        {
            "postgresql_with": {
                "fillfactor": 70,
                "autovacuum_vacuum_scale_factor": 0.02,
                "autovacuum_analyze_scale_factor": 0.05,
            }
        },
    )

    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True
    )
    state: Mapped[str] = mapped_column(server_default=ScheduleState.NEW.value)
    plans: Mapped[int] = mapped_column(server_default=text("0"))  # số nguồn đang poll
    next_build_at: Mapped[datetime | None]
    next_poll_at: Mapped[datetime | None]
    last_build_at: Mapped[datetime | None]
    last_build_status: Mapped[str | None]
    # Mốc bắt đầu lượt poll gần nhất / lượt poll đủ (mọi nguồn OK) gần nhất: tin đăng trước mốc
    # `last_poll_ok_at` chắc chắn đã được thấy — dùng đo độ tươi.
    last_poll_at: Mapped[datetime | None]
    last_poll_ok_at: Mapped[datetime | None]
    last_poll_ms: Mapped[int | None]
    last_change_at: Mapped[datetime | None]
    poll_failures: Mapped[int] = mapped_column(server_default=text("0"))
    polls: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    last_error: Mapped[str | None]
    updated_at: Mapped[datetime] = _updated_at()


class AppSetting(Base):
    """Cài đặt sửa ở giao diện (trang Cài đặt), đè lên giá trị từ biến môi trường / .env."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(primary_key=True)  # tên field của `config.Settings`
    value: Mapped[Any] = mapped_column(JSONB)
    updated_by: Mapped[str | None]
    updated_at: Mapped[datetime] = _updated_at()


class SourceFetch(Base):
    """Mỗi lần gọi một nguồn. `run_id`/`company_id` để trống với lượt index trang tuyển dụng."""

    __tablename__ = "source_fetches"
    __table_args__ = (
        one_of("outcome", FetchOutcome),
        # Dọn theo thời gian (SOURCE_FETCH_RETENTION_DAYS); bảng chỉ ghi thêm nên BRIN là đủ.
        Index("ix_source_fetches_fetched_at", "fetched_at", postgresql_using="brin"),
    )

    id: Mapped[uuid.UUID] = _pk()
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("company_runs.run_id", ondelete="CASCADE"), index=True
    )
    company_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE")
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

    id: Mapped[uuid.UUID] = _pk()
    ts: Mapped[datetime] = mapped_column(server_default=func.now())
    level: Mapped[str]
    event: Mapped[str]
    run_id: Mapped[uuid.UUID | None]
    company_id: Mapped[uuid.UUID | None]
    context: Mapped[dict[str, Any]] = mapped_column(server_default=EMPTY_OBJECT)


class FailedPage(Base):
    """HTML (nén zstd) của trang parse lỗi, để debug. Xoá sau FAILED_PAGE_RETENTION_DAYS."""

    __tablename__ = "failed_pages"

    id: Mapped[uuid.UUID] = _pk()
    url: Mapped[str]
    source: Mapped[str]
    reason: Mapped[str]
    run_id: Mapped[uuid.UUID | None]
    http_status: Mapped[int | None]
    html: Mapped[str | None] = mapped_column("html_zstd", ZstdText(Kind.HTML), comment=COMPRESSED)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), index=True)


class CompressionDict(Base):
    """Từ điển zstd huấn luyện trên dữ liệu (db/compress.py). Không xoá: giá trị cũ cần nó."""

    __tablename__ = "compression_dicts"

    # `dict_id` ngẫu nhiên ghi trong 4 byte đầu mỗi giá trị nén (sau byte phiên bản).
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    kind: Mapped[str]
    data: Mapped[bytes] = mapped_column(LargeBinary)
    samples: Mapped[int]
    created_at: Mapped[datetime]
    # Chỉ dùng để nén từ lúc này (mọi process đã kịp nạp từ điển).
    usable_at: Mapped[datetime]
