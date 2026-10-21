"""`jobs.categories`, `jobs.education`, `jobs.openings`: ngành nghề, học vấn, số lượng tuyển.

Ngành nghề là tên trong danh mục chung (`pipeline/categories.py`), lấy từ danh mục / nhãn ngành
nguồn ghi và tiêu đề; học vấn là mức tối thiểu tin yêu cầu (`EducationLevel`). Việc cũ để trống
(`{}`, `unknown`) tới lượt chạy sau của doanh nghiệp, hoặc chạy `jobcrawler db reclassify`.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column(
            "categories",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
    )
    op.add_column(
        "jobs", sa.Column("education", sa.Text(), server_default="unknown", nullable=False)
    )
    op.add_column("jobs", sa.Column("openings", sa.Integer(), nullable=True))
    op.create_check_constraint(
        op.f("ck_jobs_education"),
        "jobs",
        "education IN ('none', 'high_school', 'vocational', 'college', 'bachelor', "
        "'postgraduate', 'unknown')",
    )
    op.create_index(
        "ix_jobs_categories", "jobs", ["categories"], unique=False, postgresql_using="gin"
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_categories", table_name="jobs", postgresql_using="gin")
    op.drop_constraint(op.f("ck_jobs_education"), "jobs", type_="check")
    op.drop_column("jobs", "openings")
    op.drop_column("jobs", "education")
    op.drop_column("jobs", "categories")
