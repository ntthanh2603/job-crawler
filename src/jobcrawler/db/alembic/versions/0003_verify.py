"""Kết quả xác minh trên từng tin (job_postings) để lưu lý do nhận/loại.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-17 14:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("job_postings", sa.Column("company_id", sa.BigInteger(), nullable=True))
    op.add_column("job_postings", sa.Column("accepted", sa.Boolean(), nullable=True))
    op.add_column("job_postings", sa.Column("confidence", sa.Float(), nullable=True))
    op.add_column(
        "job_postings",
        sa.Column(
            "verification_reasons",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
    )
    op.add_column(
        "job_postings", sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        op.f("ix_job_postings_company_id"), "job_postings", ["company_id"], unique=False
    )
    op.create_foreign_key(
        op.f("fk_job_postings_company_id_companies"),
        "job_postings",
        "companies",
        ["company_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_job_postings_company_id_companies"), "job_postings", type_="foreignkey"
    )
    op.drop_index(op.f("ix_job_postings_company_id"), table_name="job_postings")
    op.drop_column("job_postings", "verified_at")
    op.drop_column("job_postings", "verification_reasons")
    op.drop_column("job_postings", "confidence")
    op.drop_column("job_postings", "accepted")
    op.drop_column("job_postings", "company_id")
