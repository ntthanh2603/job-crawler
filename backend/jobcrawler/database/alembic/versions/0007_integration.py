"""Tích hợp hệ thống ngoài (modules/integration): client + khoá API, mã doanh nghiệp bên ngoài,
outbox sự kiện việc làm.

- `api_clients` / `api_keys`: hệ thống ngoài gọi `/api/integration/v1` bằng khoá riêng (chỉ lưu
  sha256), nhận webhook ký HMAC (URL / secret mã hoá bằng secretbox).
- `company_refs`: (client, mã bên ngoài) → `companies`.
- `job_events` + `job_event_counter`: sự kiện ghi cùng transaction đổi `jobs`, relay gán `seq` liên
  tục theo thứ tự đã commit; một dòng đếm duy nhất (`id = 1`).
- `companies.input_careers_url` (trang tuyển dụng biết sẵn), `jobs.content_hash` (chỉ phát
  `updated` khi nội dung đổi; NULL ở dữ liệu cũ = chưa có mốc).

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_clients",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuid_v7()"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "scopes",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{companies:write,jobs:read}'::text[]"),
            nullable=False,
        ),
        sa.Column("all_companies", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "include_description", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column("webhook_url", sa.Text(), nullable=True),
        sa.Column("webhook_secret", sa.Text(), nullable=True),
        sa.Column("webhook_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("webhook_cursor", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("webhook_last_ok_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("webhook_failing_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("webhook_last_error", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_clients")),
        sa.UniqueConstraint("name", name=op.f("uq_api_clients_name")),
    )
    op.create_table(
        "job_event_counter",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("last", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("pruned", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_job_event_counter_single_row")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_event_counter")),
    )
    op.create_table(
        "job_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=True),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "type IN ('created', 'updated', 'closed', 'reopened', 'deleted')",
            name=op.f("ck_job_events_type"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_events")),
        sa.UniqueConstraint("seq", name=op.f("uq_job_events_seq")),
    )
    op.create_index(
        "ix_job_events_created_at",
        "job_events",
        ["created_at"],
        unique=False,
        postgresql_using="brin",
    )
    op.create_index(
        "ix_job_events_unsequenced",
        "job_events",
        ["id"],
        unique=False,
        postgresql_where=sa.text("seq IS NULL"),
    )
    op.create_table(
        "api_keys",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuid_v7()"), nullable=False),
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("prefix", sa.Text(), nullable=False),
        sa.Column("digest", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["api_clients.id"],
            name=op.f("fk_api_keys_client_id_api_clients"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
        sa.UniqueConstraint("digest", name=op.f("uq_api_keys_digest")),
    )
    op.create_index(op.f("ix_api_keys_client_id"), "api_keys", ["client_id"], unique=False)
    op.create_table(
        "company_refs",
        sa.Column("client_id", sa.UUID(), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
            ["client_id"],
            ["api_clients.id"],
            name=op.f("fk_company_refs_client_id_api_clients"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_company_refs_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("client_id", "external_id", name=op.f("pk_company_refs")),
    )
    op.create_index(
        "ix_company_refs_company_id_client_id",
        "company_refs",
        ["company_id", "client_id"],
        unique=False,
    )
    op.add_column("companies", sa.Column("input_careers_url", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("content_hash", sa.Text(), nullable=True))
    op.execute("INSERT INTO job_event_counter (id) VALUES (1)")


def downgrade() -> None:
    op.drop_column("jobs", "content_hash")
    op.drop_column("companies", "input_careers_url")
    op.drop_index("ix_company_refs_company_id_client_id", table_name="company_refs")
    op.drop_table("company_refs")
    op.drop_index(op.f("ix_api_keys_client_id"), table_name="api_keys")
    op.drop_table("api_keys")
    op.drop_index(
        "ix_job_events_unsequenced",
        table_name="job_events",
        postgresql_where=sa.text("seq IS NULL"),
    )
    op.drop_index("ix_job_events_created_at", table_name="job_events", postgresql_using="brin")
    op.drop_table("job_events")
    op.drop_table("job_event_counter")
    op.drop_table("api_clients")
