"""Bảng `audit_events`: nhật ký thao tác của người vận hành (giao diện / API).

Chỉ ghi thêm (`services/audit.py`): không có đường sửa / xoá, bảo trì định kỳ không dọn bảng này.
Khoá chính UUIDv7 (tăng theo thời gian: trang danh sách phân trang theo `id` giảm dần), `changes`
= {key: {before, after}} với bí mật đã che, `metadata` = thông tin nhỏ kèm theo.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-19
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
        "audit_events",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuid_v7()"), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("resource_type", sa.Text(), nullable=True),
        sa.Column("resource_id", sa.Text(), nullable=True),
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.Column("changes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    op.create_index("ix_audit_events_occurred_at", "audit_events", ["occurred_at"], unique=False)
    op.create_index(
        "ix_audit_events_resource_type_resource_id_occurred_at",
        "audit_events",
        ["resource_type", "resource_id", "occurred_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_audit_events_resource_type_resource_id_occurred_at", table_name="audit_events"
    )
    op.drop_index("ix_audit_events_occurred_at", table_name="audit_events")
    op.drop_table("audit_events")
