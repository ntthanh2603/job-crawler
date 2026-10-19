"""Schema ban đầu của jobcrawler (một migration duy nhất, dựng lại từ đầu ngày 19/10/2026).

Bảng chính:
- `companies`, `company_aliases`: doanh nghiệp đầu vào + KYC, tên gọi để tra cache.
- `crawl_plans`: bộ khung thu thập từng nguồn (ghi ở lượt build ban đêm, lượt poll ban ngày đi
  theo); `company_schedules`: lịch build/poll và độ tươi của từng doanh nghiệp.
- `source_profiles`, `job_postings`, `jobs`, `job_reviews`: profile trên trang tuyển dụng, từng
  tin trên từng nguồn, việc đã gộp, kiểm tay.
- `company_runs`, `source_fetches`, `failed_pages`, `logs`, `board_index_runs`: quan sát.
- `app_settings`: cài đặt sửa ở giao diện; `compression_dicts`: từ điển zstd.

Quy ước: khoá chính UUIDv7 (ứng dụng sinh, `uuid_v7()` cho SQL thô), thời gian `timestamptz`,
tên ràng buộc theo `models.NAMING_CONVENTION`, cột lớn nén zstd ở ứng dụng (`STORAGE EXTERNAL`).

Revision ID: 0001
Revises:
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# pg_trgm: tìm theo tên / tiêu đề gần giống (GIN trigram); unaccent: bỏ dấu trong SQL (đặc tả).
EXTENSIONS = ("pg_trgm", "unaccent")

# Như hàm uuidv7() có sẵn từ Postgres 18: lấy UUIDv4 ngẫu nhiên, ghi đè 48 bit đầu bằng thời gian
# (ms) và đổi version 4 → 7 (bật bit 52, 53). Cùng định dạng với `db/ids.uuid7()`.
UUID_V7 = """
CREATE OR REPLACE FUNCTION uuid_v7() RETURNS uuid LANGUAGE sql VOLATILE AS $$
  SELECT encode(
    set_bit(set_bit(
      overlay(uuid_send(gen_random_uuid())
              PLACING substring(int8send(floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint)
                                FROM 3)
              FROM 1 FOR 6),
      52, 1), 53, 1),
    'hex')::uuid
$$
"""

COMPRESSED_COLUMNS = (
    ("job_postings", "raw"),
    ("jobs", "description_md"),
    ("jobs", "requirements_md"),
    ("jobs", "benefits_md"),
    ("failed_pages", "html_zstd"),
)


def upgrade() -> None:
    for ext in EXTENSIONS:
        op.execute(f"CREATE EXTENSION IF NOT EXISTS {ext}")
    op.execute(UUID_V7)
    op.create_table(
        "app_settings",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_app_settings")),
    )
    op.create_table(
        "board_index_runs",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuid_v7()"), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), server_default="running", nullable=False),
        sa.Column("complete", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("urls_seen", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("fetched", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("new", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("updated", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("unchanged", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("failed", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("removed", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("profiles_new", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("profiles_matched", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "stats",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('running', 'ok', 'partial', 'failed', 'ambiguous')",
            name=op.f("ck_board_index_runs_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_board_index_runs")),
    )
    op.create_index(
        "ix_board_index_runs_source_started_at",
        "board_index_runs",
        ["source", "started_at"],
        unique=False,
    )
    op.create_table(
        "companies",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuid_v7()"), nullable=False),
        sa.Column("input_name", sa.Text(), nullable=False),
        sa.Column("input_website", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("legal_name", sa.Text(), nullable=True),
        sa.Column("brand_name", sa.Text(), nullable=True),
        sa.Column("tax_code", sa.Text(), nullable=True),
        sa.Column("primary_domain", sa.Text(), nullable=True),
        sa.Column(
            "other_domains",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("kyc_status", sa.Text(), nullable=True),
        sa.Column("kyc_confidence", sa.Float(), nullable=True),
        sa.Column("kyc_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("kyc_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active_jobs", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_run_id", sa.UUID(), nullable=True),
        sa.Column("last_run_status", sa.Text(), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "last_run_incomplete",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column(
            "search_text",
            sa.Text(),
            sa.Computed(
                "coalesce(input_name, '') || ' ' || coalesce(legal_name, '') || ' ' || coalesce(brand_name, '') || ' ' || coalesce(tax_code, '') || ' ' || coalesce(primary_domain, '')",
                persisted=True,
            ),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kyc_status IN ('verified', 'ambiguous', 'not_found')",
            name=op.f("ck_companies_kyc_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_companies")),
        sa.UniqueConstraint("tax_code", name=op.f("uq_companies_tax_code")),
    )
    op.create_index("ix_companies_kyc_status", "companies", ["kyc_status"], unique=False)
    op.create_index("ix_companies_last_run_status", "companies", ["last_run_status"], unique=False)
    op.create_index(
        op.f("ix_companies_primary_domain"), "companies", ["primary_domain"], unique=False
    )
    op.create_index(
        "ix_companies_search_text",
        "companies",
        ["search_text"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"search_text": "gin_trgm_ops"},
    )
    op.create_table(
        "compression_dicts",
        sa.Column("id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("samples", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("usable_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_compression_dicts")),
    )
    op.create_table(
        "failed_pages",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuid_v7()"), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column(
            "html_zstd",
            sa.LargeBinary(),
            nullable=True,
            comment="zstd (db/compress.py): 1 byte phiên bản + 4 byte id từ điển + frame zstd",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_failed_pages")),
    )
    op.create_index(
        op.f("ix_failed_pages_created_at"), "failed_pages", ["created_at"], unique=False
    )
    op.create_table(
        "logs",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuid_v7()"), nullable=False),
        sa.Column(
            "ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("company_id", sa.UUID(), nullable=True),
        sa.Column(
            "context",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_logs")),
    )
    op.create_index(
        "ix_logs_run_id",
        "logs",
        ["run_id"],
        unique=False,
        postgresql_where=sa.text("run_id IS NOT NULL"),
    )
    op.create_index("ix_logs_ts", "logs", ["ts"], unique=False, postgresql_using="brin")
    op.create_table(
        "company_aliases",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuid_v7()"), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("alias", sa.Text(), nullable=False),
        sa.Column("alias_norm", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_company_aliases_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_company_aliases")),
        sa.UniqueConstraint(
            "company_id", "alias_norm", name=op.f("uq_company_aliases_company_id_alias_norm")
        ),
    )
    op.create_index(
        "ix_company_aliases_alias_norm", "company_aliases", ["alias_norm"], unique=False
    )
    op.create_index(
        "ix_company_aliases_alias_norm_trgm",
        "company_aliases",
        ["alias_norm"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"alias_norm": "gin_trgm_ops"},
    )
    op.create_index(
        op.f("ix_company_aliases_company_id"), "company_aliases", ["company_id"], unique=False
    )
    op.create_table(
        "company_runs",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.Text(), server_default="build", nullable=False),
        sa.Column("trigger", sa.Text(), server_default="manual", nullable=False),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), server_default="running", nullable=False),
        sa.Column("jobs_found", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("jobs_accepted", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("jobs_new", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("jobs_closed", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "incomplete_sources",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint("kind IN ('build', 'poll')", name=op.f("ck_company_runs_kind")),
        sa.CheckConstraint(
            "status IN ('running', 'ok', 'partial', 'failed', 'ambiguous')",
            name=op.f("ck_company_runs_status"),
        ),
        sa.CheckConstraint(
            "trigger IN ('nightly', 'schedule', 'error', 'board', 'new', 'manual', 'continuation')",
            name=op.f("ck_company_runs_trigger"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_company_runs_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_company_runs")),
    )
    op.create_index(
        "ix_company_runs_company_id_started_at",
        "company_runs",
        ["company_id", "started_at"],
        unique=False,
    )
    op.create_index(
        "ix_company_runs_kind_started_at", "company_runs", ["kind", "started_at"], unique=False
    )
    op.create_index("ix_company_runs_started_at", "company_runs", ["started_at"], unique=False)
    op.create_table(
        "company_schedules",
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("state", sa.Text(), server_default="new", nullable=False),
        sa.Column("plans", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_build_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_build_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_build_status", sa.Text(), nullable=True),
        sa.Column("last_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_poll_ok_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_poll_ms", sa.Integer(), nullable=True),
        sa.Column("last_change_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("poll_failures", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("polls", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('new', 'ready', 'no_sources', 'broken', 'paused')",
            name=op.f("ck_company_schedules_state"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_company_schedules_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("company_id", name=op.f("pk_company_schedules")),
        postgresql_with={
            "fillfactor": 70,
            "autovacuum_vacuum_scale_factor": 0.02,
            "autovacuum_analyze_scale_factor": 0.05,
        },
    )
    op.create_index(
        "ix_company_schedules_next_build_at", "company_schedules", ["next_build_at"], unique=False
    )
    op.create_index(
        "ix_company_schedules_next_poll_at",
        "company_schedules",
        ["next_poll_at"],
        unique=False,
        postgresql_where=sa.text("state = 'ready'"),
    )
    op.create_table(
        "crawl_plans",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuid_v7()"), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column(
            "spec",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column("jobs", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "job_keys",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("signature", sa.Text(), nullable=True),
        sa.Column("built_run_id", sa.UUID(), nullable=True),
        sa.Column("built_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failures", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('active', 'broken', 'retired')", name=op.f("ck_crawl_plans_status")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_crawl_plans_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_crawl_plans")),
        sa.UniqueConstraint("company_id", "source", name=op.f("uq_crawl_plans_company_id_source")),
    )
    op.create_table(
        "source_profiles",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuid_v7()"), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("name_on_source", sa.Text(), nullable=False),
        sa.Column("name_norm", sa.Text(), nullable=True),
        sa.Column("website", sa.Text(), nullable=True),
        sa.Column("tax_code", sa.Text(), nullable=True),
        sa.Column(
            "info",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("company_id", sa.UUID(), nullable=True),
        sa.Column("match_confidence", sa.Float(), nullable=True),
        sa.Column("match_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("matched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_source_profiles_company_id_companies"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_profiles")),
        sa.UniqueConstraint(
            "source", "external_id", name=op.f("uq_source_profiles_source_external_id")
        ),
    )
    op.create_index(
        op.f("ix_source_profiles_company_id"), "source_profiles", ["company_id"], unique=False
    )
    op.create_index(
        "ix_source_profiles_name_norm_trgm",
        "source_profiles",
        ["name_norm"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"name_norm": "gin_trgm_ops"},
    )
    op.create_table(
        "job_postings",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuid_v7()"), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("source_profile_id", sa.UUID(), nullable=True),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column(
            "raw",
            sa.LargeBinary(),
            nullable=False,
            comment="zstd (db/compress.py): 1 byte phiên bản + 4 byte id từ điển + frame zstd",
        ),
        sa.Column("content_hash", sa.Text(), nullable=True),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column("lastmod", sa.DateTime(timezone=True), nullable=True),
        sa.Column("etag", sa.Text(), nullable=True),
        sa.Column("http_last_modified", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("missed_runs", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=True),
        sa.Column("accepted", sa.Boolean(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column(
            "verification_reasons",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'expired', 'removed')", name=op.f("ck_job_postings_status")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_job_postings_company_id_companies"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_profile_id"],
            ["source_profiles.id"],
            name=op.f("fk_job_postings_source_profile_id_source_profiles"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_postings")),
        sa.UniqueConstraint(
            "source", "external_id", name=op.f("uq_job_postings_source_external_id")
        ),
    )
    op.create_index(
        op.f("ix_job_postings_company_id"), "job_postings", ["company_id"], unique=False
    )
    op.create_index(
        "ix_job_postings_company_rejected",
        "job_postings",
        ["company_id", sa.literal_column("verified_at DESC")],
        unique=False,
        postgresql_where=sa.text("accepted IS false"),
    )
    op.create_index(op.f("ix_job_postings_job_id"), "job_postings", ["job_id"], unique=False)
    op.create_index(
        op.f("ix_job_postings_source_profile_id"),
        "job_postings",
        ["source_profile_id"],
        unique=False,
    )
    op.create_table(
        "source_fetches",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuid_v7()"), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("company_id", sa.UUID(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("proxy_id", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("items", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "outcome IN ('ok', 'blocked', 'timeout', 'error', 'robots_disallowed')",
            name=op.f("ck_source_fetches_outcome"),
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_source_fetches_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["company_runs.run_id"],
            name=op.f("fk_source_fetches_run_id_company_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_fetches")),
    )
    op.create_index(
        "ix_source_fetches_fetched_at",
        "source_fetches",
        ["fetched_at"],
        unique=False,
        postgresql_using="brin",
    )
    op.create_index(op.f("ix_source_fetches_run_id"), "source_fetches", ["run_id"], unique=False)
    op.create_table(
        "jobs",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuid_v7()"), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("cluster_key", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=True),
        sa.Column("canonical_posting_id", sa.UUID(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("title_normalized", sa.Text(), nullable=False),
        sa.Column("company_name", sa.Text(), nullable=False),
        sa.Column("level", sa.Text(), server_default="unknown", nullable=False),
        sa.Column("employment_type", sa.Text(), server_default="unknown", nullable=False),
        sa.Column("work_mode", sa.Text(), server_default="unknown", nullable=False),
        sa.Column(
            "locations",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("salary_min", sa.Float(), nullable=True),
        sa.Column("salary_max", sa.Float(), nullable=True),
        sa.Column("salary_currency", sa.Text(), nullable=True),
        sa.Column("salary_period", sa.Text(), nullable=True),
        sa.Column(
            "salary_negotiable", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("salary_raw", sa.Text(), nullable=True),
        sa.Column("experience_years_min", sa.Float(), nullable=True),
        sa.Column("experience_years_max", sa.Float(), nullable=True),
        sa.Column(
            "description_md",
            sa.LargeBinary(),
            nullable=False,
            comment="zstd (db/compress.py): 1 byte phiên bản + 4 byte id từ điển + frame zstd",
        ),
        sa.Column(
            "requirements_md",
            sa.LargeBinary(),
            nullable=True,
            comment="zstd (db/compress.py): 1 byte phiên bản + 4 byte id từ điển + frame zstd",
        ),
        sa.Column(
            "benefits_md",
            sa.LargeBinary(),
            nullable=True,
            comment="zstd (db/compress.py): 1 byte phiên bản + 4 byte id từ điển + frame zstd",
        ),
        sa.Column(
            "skills",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("apply_url", sa.Text(), nullable=True),
        sa.Column(
            "source_urls",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("canonical_source", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column(
            "verification_reasons",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "employment_type IN ('full_time', 'part_time', 'contract', 'internship', 'freelance', 'unknown')",
            name=op.f("ck_jobs_employment_type"),
        ),
        sa.CheckConstraint(
            "level IN ('intern', 'fresher', 'junior', 'middle', 'senior', 'lead', 'manager', 'director', 'unknown')",
            name=op.f("ck_jobs_level"),
        ),
        sa.CheckConstraint(
            "salary_period IN ('month', 'year', 'hour')", name=op.f("ck_jobs_salary_period")
        ),
        sa.CheckConstraint(
            "status IN ('active', 'expired', 'removed')", name=op.f("ck_jobs_status")
        ),
        sa.CheckConstraint(
            "work_mode IN ('onsite', 'hybrid', 'remote', 'unknown')", name=op.f("ck_jobs_work_mode")
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name=op.f("ck_jobs_confidence_range")
        ),
        sa.ForeignKeyConstraint(
            ["canonical_posting_id"],
            ["job_postings.id"],
            name=op.f("fk_jobs_canonical_posting_id_job_postings"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_jobs_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
        sa.UniqueConstraint(
            "company_id", "cluster_key", name=op.f("uq_jobs_company_id_cluster_key")
        ),
    )
    op.create_index(
        "ix_jobs_company_id_status",
        "jobs",
        ["company_id", "status", sa.literal_column("first_seen_at DESC")],
        unique=False,
    )
    op.create_index(
        "ix_jobs_company_name_trgm",
        "jobs",
        ["company_name"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"company_name": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_jobs_first_seen_at_brin",
        "jobs",
        ["first_seen_at"],
        unique=False,
        postgresql_using="brin",
    )
    op.create_index(
        "ix_jobs_locations",
        "jobs",
        ["locations"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"locations": "jsonb_path_ops"},
    )
    op.create_index(
        "ix_jobs_status_expires_at",
        "jobs",
        ["status", "expires_at", sa.literal_column("id DESC")],
        unique=False,
    )
    op.create_index(
        "ix_jobs_status_first_seen_at",
        "jobs",
        ["status", sa.literal_column("first_seen_at DESC"), sa.literal_column("id DESC")],
        unique=False,
    )
    op.create_index(
        "ix_jobs_status_posted_at",
        "jobs",
        ["status", sa.literal_column("posted_at DESC NULLS LAST"), sa.literal_column("id DESC")],
        unique=False,
    )
    op.create_index(
        "ix_jobs_status_salary",
        "jobs",
        [
            "status",
            sa.literal_column("coalesce(salary_max, salary_min) DESC NULLS LAST"),
            sa.literal_column("id DESC"),
        ],
        unique=False,
    )
    op.create_index(
        "ix_jobs_status_source_kind",
        "jobs",
        ["status", sa.literal_column("split_part(canonical_source, ':', 1)")],
        unique=False,
    )
    op.create_index(
        "ix_jobs_title_trgm",
        "jobs",
        ["title"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"title": "gin_trgm_ops"},
    )
    op.create_index(
        "ux_jobs_company_fingerprint_active",
        "jobs",
        ["company_id", "fingerprint"],
        unique=True,
        postgresql_where=sa.text("status = 'active' AND fingerprint IS NOT NULL"),
    )
    op.create_table(
        "job_reviews",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuid_v7()"), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("correct", sa.Boolean(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("reviewer", sa.Text(), nullable=True),
        sa.Column(
            "reviewed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=op.f("fk_job_reviews_job_id_jobs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_reviews")),
        sa.UniqueConstraint("job_id", name=op.f("uq_job_reviews_job_id")),
    )
    # job_postings.job_id ↔ jobs.canonical_posting_id tham chiếu vòng: khoá này tạo sau cả hai bảng.
    op.create_foreign_key(
        op.f("fk_job_postings_job_id_jobs"),
        "job_postings",
        "jobs",
        ["job_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Cột đã nén zstd sẵn (db/compress.py): Postgres không nén lại, đọc được phần đầu giá trị.
    for table, column in COMPRESSED_COLUMNS:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET STORAGE EXTERNAL")


def downgrade() -> None:
    op.drop_constraint(op.f("fk_job_postings_job_id_jobs"), "job_postings", type_="foreignkey")
    op.drop_table("job_reviews")
    op.drop_index(
        "ux_jobs_company_fingerprint_active",
        table_name="jobs",
        postgresql_where=sa.text("status = 'active' AND fingerprint IS NOT NULL"),
    )
    op.drop_index(
        "ix_jobs_title_trgm",
        table_name="jobs",
        postgresql_using="gin",
        postgresql_ops={"title": "gin_trgm_ops"},
    )
    op.drop_index("ix_jobs_status_source_kind", table_name="jobs")
    op.drop_index("ix_jobs_status_salary", table_name="jobs")
    op.drop_index("ix_jobs_status_posted_at", table_name="jobs")
    op.drop_index("ix_jobs_status_first_seen_at", table_name="jobs")
    op.drop_index("ix_jobs_status_expires_at", table_name="jobs")
    op.drop_index(
        "ix_jobs_locations",
        table_name="jobs",
        postgresql_using="gin",
        postgresql_ops={"locations": "jsonb_path_ops"},
    )
    op.drop_index("ix_jobs_first_seen_at_brin", table_name="jobs", postgresql_using="brin")
    op.drop_index(
        "ix_jobs_company_name_trgm",
        table_name="jobs",
        postgresql_using="gin",
        postgresql_ops={"company_name": "gin_trgm_ops"},
    )
    op.drop_index("ix_jobs_company_id_status", table_name="jobs")
    op.drop_table("jobs")
    op.drop_index(op.f("ix_source_fetches_run_id"), table_name="source_fetches")
    op.drop_index(
        "ix_source_fetches_fetched_at", table_name="source_fetches", postgresql_using="brin"
    )
    op.drop_table("source_fetches")
    op.drop_index(op.f("ix_job_postings_source_profile_id"), table_name="job_postings")
    op.drop_index(op.f("ix_job_postings_job_id"), table_name="job_postings")
    op.drop_index(
        "ix_job_postings_company_rejected",
        table_name="job_postings",
        postgresql_where=sa.text("accepted IS false"),
    )
    op.drop_index(op.f("ix_job_postings_company_id"), table_name="job_postings")
    op.drop_table("job_postings")
    op.drop_index(
        "ix_source_profiles_name_norm_trgm",
        table_name="source_profiles",
        postgresql_using="gin",
        postgresql_ops={"name_norm": "gin_trgm_ops"},
    )
    op.drop_index(op.f("ix_source_profiles_company_id"), table_name="source_profiles")
    op.drop_table("source_profiles")
    op.drop_table("crawl_plans")
    op.drop_index(
        "ix_company_schedules_next_poll_at",
        table_name="company_schedules",
        postgresql_where=sa.text("state = 'ready'"),
    )
    op.drop_index("ix_company_schedules_next_build_at", table_name="company_schedules")
    op.drop_table("company_schedules")
    op.drop_index("ix_company_runs_started_at", table_name="company_runs")
    op.drop_index("ix_company_runs_kind_started_at", table_name="company_runs")
    op.drop_index("ix_company_runs_company_id_started_at", table_name="company_runs")
    op.drop_table("company_runs")
    op.drop_index(op.f("ix_company_aliases_company_id"), table_name="company_aliases")
    op.drop_index(
        "ix_company_aliases_alias_norm_trgm",
        table_name="company_aliases",
        postgresql_using="gin",
        postgresql_ops={"alias_norm": "gin_trgm_ops"},
    )
    op.drop_index("ix_company_aliases_alias_norm", table_name="company_aliases")
    op.drop_table("company_aliases")
    op.drop_index("ix_logs_ts", table_name="logs", postgresql_using="brin")
    op.drop_index(
        "ix_logs_run_id", table_name="logs", postgresql_where=sa.text("run_id IS NOT NULL")
    )
    op.drop_table("logs")
    op.drop_index(op.f("ix_failed_pages_created_at"), table_name="failed_pages")
    op.drop_table("failed_pages")
    op.drop_table("compression_dicts")
    op.drop_index(
        "ix_companies_search_text",
        table_name="companies",
        postgresql_using="gin",
        postgresql_ops={"search_text": "gin_trgm_ops"},
    )
    op.drop_index(op.f("ix_companies_primary_domain"), table_name="companies")
    op.drop_index("ix_companies_last_run_status", table_name="companies")
    op.drop_index("ix_companies_kyc_status", table_name="companies")
    op.drop_table("companies")
    op.drop_index("ix_board_index_runs_source_started_at", table_name="board_index_runs")
    op.drop_table("board_index_runs")
    op.drop_table("app_settings")
    # Extension (pg_trgm, unaccent) giữ lại: có thể DB khác trên cùng server đang dùng.
    op.execute("DROP FUNCTION IF EXISTS uuid_v7()")
