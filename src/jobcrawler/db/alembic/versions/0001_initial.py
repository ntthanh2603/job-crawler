"""Schema ban đầu: doanh nghiệp, nguồn, tin, job, lượt chạy, log.

Revision ID: 0001
Revises:
Create Date: 2026-10-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


EXTENSIONS = ("pg_trgm", "unaccent", "vector")


def upgrade() -> None:
    for ext in EXTENSIONS:
        op.execute(f"CREATE EXTENSION IF NOT EXISTS {ext}")

    op.create_table(
        "companies",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("input_name", sa.Text(), nullable=False),
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
    op.create_index(
        op.f("ix_companies_primary_domain"), "companies", ["primary_domain"], unique=False
    )
    op.create_table(
        "failed_pages",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("html_zstd", sa.LargeBinary(), nullable=True),
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
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("company_id", sa.BigInteger(), nullable=True),
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
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("company_id", sa.BigInteger(), nullable=False),
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
        sa.Column("company_id", sa.BigInteger(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), server_default="running", nullable=False),
        sa.Column("jobs_found", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("jobs_accepted", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "incomplete_sources",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'ok', 'partial', 'failed', 'ambiguous')",
            name=op.f("ck_company_runs_status"),
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
    op.create_table(
        "source_profiles",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("name_on_source", sa.Text(), nullable=False),
        sa.Column("company_id", sa.BigInteger(), nullable=True),
        sa.Column("match_confidence", sa.Float(), nullable=True),
        sa.Column("match_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
    op.create_table(
        "job_postings",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("source_profile_id", sa.BigInteger(), nullable=True),
        sa.Column("job_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "raw",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
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
        sa.CheckConstraint(
            "status IN ('active', 'expired', 'removed')", name=op.f("ck_job_postings_status")
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
    op.create_index(op.f("ix_job_postings_job_id"), "job_postings", ["job_id"], unique=False)
    op.create_index(
        op.f("ix_job_postings_source_profile_id"),
        "job_postings",
        ["source_profile_id"],
        unique=False,
    )
    op.create_table(
        "source_fetches",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("company_id", sa.BigInteger(), nullable=True),
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
    op.create_index(op.f("ix_source_fetches_run_id"), "source_fetches", ["run_id"], unique=False)
    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("company_id", sa.BigInteger(), nullable=False),
        sa.Column("cluster_key", sa.Text(), nullable=False),
        sa.Column("canonical_posting_id", sa.BigInteger(), nullable=True),
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
        sa.Column("description_md", sa.Text(), nullable=False),
        sa.Column("requirements_md", sa.Text(), nullable=True),
        sa.Column("benefits_md", sa.Text(), nullable=True),
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
    op.create_index(op.f("ix_jobs_company_id"), "jobs", ["company_id"], unique=False)
    # job_postings.job_id ↔ jobs.canonical_posting_id tham chiếu vòng: thêm FK sau khi có cả hai.
    op.create_foreign_key(
        op.f("fk_job_postings_job_id_jobs"),
        "job_postings",
        "jobs",
        ["job_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    # Giữ lại extension: có thể đang được dùng ngoài schema này và cần quyền superuser để xoá.
    op.drop_constraint(op.f("fk_job_postings_job_id_jobs"), "job_postings", type_="foreignkey")
    op.drop_index(op.f("ix_jobs_company_id"), table_name="jobs")
    op.drop_table("jobs")
    op.drop_index(op.f("ix_source_fetches_run_id"), table_name="source_fetches")
    op.drop_table("source_fetches")
    op.drop_index(op.f("ix_job_postings_source_profile_id"), table_name="job_postings")
    op.drop_index(op.f("ix_job_postings_job_id"), table_name="job_postings")
    op.drop_table("job_postings")
    op.drop_index(op.f("ix_source_profiles_company_id"), table_name="source_profiles")
    op.drop_table("source_profiles")
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
    op.drop_index(op.f("ix_companies_primary_domain"), table_name="companies")
    op.drop_table("companies")
