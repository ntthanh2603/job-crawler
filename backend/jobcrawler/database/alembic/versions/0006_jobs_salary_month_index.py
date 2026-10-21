"""`ix_jobs_status_salary` theo lương tháng VND thay vì số ghi trong tin.

Lọc "lương từ" (triệu VND / tháng) và sắp theo lương so lương năm như lương tháng, xếp ngoại tệ /
lương theo giờ lẫn với VND / tháng. Nay cả hai dùng lương tháng quy về VND
(`models.JOB_SALARY_MONTH_SQL`: lương năm chia 12, giờ / ngày / tuần / ngoại tệ là NULL); index đổi theo
biểu thức đó để danh sách vẫn sắp theo index.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Chép nguyên văn (không import từ models): revision không đổi theo mã sau này.
SALARY_MONTH = (
    "(CASE WHEN salary_currency = 'VND' AND salary_period = 'year' "
    "THEN coalesce(salary_max, salary_min) / 12 "
    "WHEN salary_currency = 'VND' AND salary_period = 'month' "
    "THEN coalesce(salary_max, salary_min) END)"
)


def _salary_index(expression: str) -> None:
    op.create_index(
        "ix_jobs_status_salary",
        "jobs",
        [
            "status",
            sa.literal_column(f"{expression} DESC NULLS LAST"),
            sa.literal_column("id DESC"),
        ],
        unique=False,
    )


def upgrade() -> None:
    op.drop_index("ix_jobs_status_salary", table_name="jobs")
    _salary_index(SALARY_MONTH)


def downgrade() -> None:
    op.drop_index("ix_jobs_status_salary", table_name="jobs")
    _salary_index("coalesce(salary_max, salary_min)")
