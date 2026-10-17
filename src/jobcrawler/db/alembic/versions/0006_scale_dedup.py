"""Chịu dữ liệu lớn + chống job trùng.

- `jobs.fingerprint` (dấu vân tay nội dung, xem pipeline/dedup.py) + index duy nhất
  (company_id, fingerprint) trong các job đang mở: hai job y hệt không thể cùng tồn tại.
- Index cho danh sách việc làm: lọc trạng thái + từng kiểu sắp xếp, nguồn, tỉnh/thành (GIN
  jsonb_path_ops), tìm kiếm tiêu đề / tên doanh nghiệp (GIN trigram), BRIN theo first_seen_at
  cho biểu đồ theo ngày. (company_id, status, first_seen_at) thay index company_id đơn.
- `companies`: lượt chạy mới nhất (last_run_*) và số job đang mở (`active_jobs`) ghi sẵn trên dòng
  doanh nghiệp, `search_text` (cột sinh) + GIN trigram — danh sách doanh nghiệp không còn truy
  vấn con theo từng dòng.
- `job_postings`: index cho tin bị loại của một doanh nghiệp.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-17 23:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEARCH_TEXT = (
    "coalesce(input_name, '') || ' ' || coalesce(legal_name, '') || ' ' || "
    "coalesce(brand_name, '') || ' ' || coalesce(tax_code, '') || ' ' || "
    "coalesce(primary_domain, '')"
)

JOB_INDEXES: list[tuple[str, str]] = [
    ("ix_jobs_status_first_seen_at", "(status, first_seen_at DESC, id DESC)"),
    ("ix_jobs_status_posted_at", "(status, posted_at DESC NULLS LAST, id DESC)"),
    ("ix_jobs_status_expires_at", "(status, expires_at, id DESC)"),
    (
        "ix_jobs_status_salary",
        "(status, coalesce(salary_max, salary_min) DESC NULLS LAST, id DESC)",
    ),
    ("ix_jobs_status_source_kind", "(status, split_part(canonical_source, ':', 1))"),
    ("ix_jobs_company_id_status", "(company_id, status, first_seen_at DESC)"),
    ("ix_jobs_locations", "USING gin (locations jsonb_path_ops)"),
    ("ix_jobs_title_trgm", "USING gin (title gin_trgm_ops)"),
    ("ix_jobs_company_name_trgm", "USING gin (company_name gin_trgm_ops)"),
    ("ix_jobs_first_seen_at_brin", "USING brin (first_seen_at)"),
]


def upgrade() -> None:
    # --- jobs ---------------------------------------------------------------------------------
    op.add_column("jobs", sa.Column("fingerprint", sa.Text(), nullable=True))
    op.create_index(
        "ux_jobs_company_fingerprint_active",
        "jobs",
        ["company_id", "fingerprint"],
        unique=True,
        postgresql_where=sa.text("status = 'active' AND fingerprint IS NOT NULL"),
    )
    op.drop_index("ix_jobs_company_id", table_name="jobs")
    for name, spec in JOB_INDEXES:
        op.execute(f"CREATE INDEX {name} ON jobs {spec}")

    # --- companies ----------------------------------------------------------------------------
    op.add_column(
        "companies",
        sa.Column("active_jobs", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "companies", sa.Column("last_run_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column("companies", sa.Column("last_run_status", sa.Text(), nullable=True))
    op.add_column("companies", sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("companies", sa.Column("last_run_duration_ms", sa.Integer(), nullable=True))
    op.add_column(
        "companies",
        sa.Column(
            "last_run_incomplete",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
    )
    op.add_column(
        "companies",
        sa.Column("search_text", sa.Text(), sa.Computed(SEARCH_TEXT, persisted=True)),
    )
    op.create_index(
        "ix_companies_search_text",
        "companies",
        ["search_text"],
        postgresql_using="gin",
        postgresql_ops={"search_text": "gin_trgm_ops"},
    )
    op.create_index("ix_companies_last_run_status", "companies", ["last_run_status"])
    op.create_index("ix_companies_kyc_status", "companies", ["kyc_status"])
    op.execute(
        """
        UPDATE companies c SET active_jobs = j.n
        FROM (SELECT company_id, count(*) AS n FROM jobs WHERE status = 'active'
              GROUP BY company_id) j
        WHERE j.company_id = c.id
        """
    )
    op.execute(
        """
        UPDATE companies c SET last_run_id = r.run_id, last_run_status = r.status,
               last_run_at = r.started_at, last_run_duration_ms = r.duration_ms,
               last_run_incomplete = r.incomplete_sources
        FROM (SELECT DISTINCT ON (company_id) company_id, run_id, status, started_at,
                     duration_ms, incomplete_sources
              FROM company_runs ORDER BY company_id, started_at DESC) r
        WHERE r.company_id = c.id
        """
    )

    # --- job_postings -------------------------------------------------------------------------
    op.create_index(
        "ix_job_postings_company_rejected",
        "job_postings",
        ["company_id", sa.text("verified_at DESC")],
        postgresql_where=sa.text("accepted IS false"),
    )


def downgrade() -> None:
    op.drop_index("ix_job_postings_company_rejected", table_name="job_postings")
    op.drop_index("ix_companies_kyc_status", table_name="companies")
    op.drop_index("ix_companies_last_run_status", table_name="companies")
    op.drop_index("ix_companies_search_text", table_name="companies")
    for column in (
        "search_text",
        "last_run_incomplete",
        "last_run_duration_ms",
        "last_run_at",
        "last_run_status",
        "last_run_id",
        "active_jobs",
    ):
        op.drop_column("companies", column)
    for name, _ in reversed(JOB_INDEXES):
        op.drop_index(name, table_name="jobs")
    op.create_index("ix_jobs_company_id", "jobs", ["company_id"])
    op.drop_index("ux_jobs_company_fingerprint_active", table_name="jobs")
    op.drop_column("jobs", "fingerprint")
