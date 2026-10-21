"""`audit_events.ip`, `audit_events.user_agent`: nguồn request của mỗi dòng nhật ký thao tác.

IP người dùng (sau reverse proxy tin cậy là IP thật) và User-Agent của trình duyệt / client, do
`GuardMiddleware` gắn vào lúc xử lý request (`modules/audit/audit_service.py`). Dòng cũ và thao
tác không đến từ request (CLI, cron) để trống.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("audit_events", sa.Column("ip", sa.Text(), nullable=True))
    op.add_column("audit_events", sa.Column("user_agent", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("audit_events", "user_agent")
    op.drop_column("audit_events", "ip")
