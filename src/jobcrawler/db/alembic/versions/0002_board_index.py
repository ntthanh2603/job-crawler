"""Index trang tuyển dụng: crawl tăng dần (lastmod, ETag), map profile, lượt index.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-17 09:01:17.640752
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "board_index_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
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
    op.add_column("job_postings", sa.Column("lastmod", sa.DateTime(timezone=True), nullable=True))
    op.add_column("job_postings", sa.Column("etag", sa.Text(), nullable=True))
    op.add_column("job_postings", sa.Column("http_last_modified", sa.Text(), nullable=True))
    op.add_column(
        "job_postings", sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "job_postings", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "job_postings",
        sa.Column("missed_runs", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column("source_profiles", sa.Column("name_norm", sa.Text(), nullable=True))
    op.add_column("source_profiles", sa.Column("website", sa.Text(), nullable=True))
    op.add_column("source_profiles", sa.Column("tax_code", sa.Text(), nullable=True))
    op.add_column(
        "source_profiles",
        sa.Column(
            "info",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "source_profiles", sa.Column("matched_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "source_profiles", sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        "ix_source_profiles_name_norm_trgm",
        "source_profiles",
        ["name_norm"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"name_norm": "gin_trgm_ops"},
    )


def downgrade() -> None:
    op.drop_index(
        "ix_source_profiles_name_norm_trgm",
        table_name="source_profiles",
        postgresql_using="gin",
        postgresql_ops={"name_norm": "gin_trgm_ops"},
    )
    op.drop_column("source_profiles", "indexed_at")
    op.drop_column("source_profiles", "matched_at")
    op.drop_column("source_profiles", "info")
    op.drop_column("source_profiles", "tax_code")
    op.drop_column("source_profiles", "website")
    op.drop_column("source_profiles", "name_norm")
    op.drop_column("job_postings", "missed_runs")
    op.drop_column("job_postings", "expires_at")
    op.drop_column("job_postings", "fetched_at")
    op.drop_column("job_postings", "http_last_modified")
    op.drop_column("job_postings", "etag")
    op.drop_column("job_postings", "lastmod")
    op.drop_index("ix_board_index_runs_source_started_at", table_name="board_index_runs")
    op.drop_table("board_index_runs")
