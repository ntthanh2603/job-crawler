"""company_runs.queued_at: lúc job của lượt chạy sẵn sàng chạy (xếp hàng, hoặc tới giờ hẹn).

`started_at - queued_at` là thời gian chờ trong hàng đợi. Để trống với lượt chạy tay (CLI `run`)
và lượt trước migration.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-19 10:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("company_runs", sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("company_runs", "queued_at")
