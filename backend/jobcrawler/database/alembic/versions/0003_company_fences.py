"""Bảng `company_fences`: fencing token của khoá theo doanh nghiệp.

Mỗi lần một lượt build / poll giữ khoá doanh nghiệp (`services/crawl.company_lock`), `fence` tăng
một; mọi commit của lượt đó kiểm `fence` còn bằng token của mình (`db/fencing.py`), nên lượt cũ đã
mất khoá (process đứng quá hạn rồi chạy tiếp) không ghi đè được dữ liệu của lượt mới. Dòng tạo ở
lần giữ khoá đầu tiên.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "company_fences",
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("fence", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_company_fences_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("company_id", name=op.f("pk_company_fences")),
    )


def downgrade() -> None:
    op.drop_table("company_fences")
