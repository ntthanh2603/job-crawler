"""M6: nén zstd các cột lớn, bảng từ điển nén, cột đầu vào của doanh nghiệp, index để dọn dữ
liệu cũ.

`job_postings.raw` (jsonb) và `jobs.description_md/requirements_md/benefits_md` (text) chuyển
thành bytea nén (định dạng ở `jobcrawler.db.compress`); dữ liệu cũ được nén ngay trong migration
(không từ điển — lượt bảo trì sau sẽ huấn luyện từ điển và nén lại).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-17 18:00:00
"""

import json
from collections.abc import Callable, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from jobcrawler.db.compress import Codec, Kind, dumps, load_dictionaries_sync

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COMMENT = "zstd (db/compress.py): 1 byte phiên bản + 4 byte id từ điển + frame zstd"
BATCH = 1000

# (bảng, cột, loại nén, nullable, kiểu cũ, cách đọc giá trị cũ trong SELECT)
COLUMNS: list[tuple[str, str, Kind, bool, sa.types.TypeEngine[Any], str]] = [
    ("job_postings", "raw", Kind.POSTING, False, postgresql.JSONB(), "raw::text"),
    ("jobs", "description_md", Kind.JOB_TEXT, False, sa.Text(), "description_md"),
    ("jobs", "requirements_md", Kind.JOB_TEXT, True, sa.Text(), "requirements_md"),
    ("jobs", "benefits_md", Kind.JOB_TEXT, True, sa.Text(), "benefits_md"),
]


def _convert(
    table: str,
    source: str,
    target: str,
    transform: Callable[[Any], Any],
) -> None:
    """Đọc `source`, ghi `transform(giá trị)` vào cột `target` theo lô."""
    conn = op.get_bind()
    select = sa.text(f"SELECT id, {source} FROM {table} WHERE id > :last ORDER BY id LIMIT {BATCH}")
    update = sa.text(f"UPDATE {table} SET {target} = :value WHERE id = :id")
    last = 0
    while rows := conn.execute(select, {"last": last}).all():
        conn.execute(
            update,
            [{"id": i, "value": None if v is None else transform(v)} for i, v in rows],
        )
        last = rows[-1][0]


def upgrade() -> None:
    op.create_table(
        "compression_dicts",
        sa.Column("id", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("samples", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("usable_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_compression_dicts")),
    )
    op.add_column("companies", sa.Column("input_website", sa.Text(), nullable=True))
    op.add_column("companies", sa.Column("note", sa.Text(), nullable=True))
    op.add_column("failed_pages", sa.Column("run_id", sa.Uuid(), nullable=True))
    op.add_column("failed_pages", sa.Column("http_status", sa.Integer(), nullable=True))
    op.alter_column("failed_pages", "html_zstd", comment=COMMENT)
    op.execute("ALTER TABLE failed_pages ALTER COLUMN html_zstd SET STORAGE EXTERNAL")
    op.create_index("ix_company_runs_started_at", "company_runs", ["started_at"])
    op.create_index(
        "ix_source_fetches_fetched_at",
        "source_fetches",
        ["fetched_at"],
        postgresql_using="brin",
    )

    codec = Codec(level=9)
    for table, column, kind, nullable, _old, source in COLUMNS:
        tmp = f"{column}_z"
        op.add_column(table, sa.Column(tmp, sa.LargeBinary(), nullable=True))
        if kind is Kind.POSTING:
            # jsonb::text có khoảng trắng; ghi JSON gọn như ứng dụng.
            _convert(table, source, tmp, lambda v, k=kind: codec.encode(k, dumps(json.loads(v))))
        else:
            _convert(table, source, tmp, lambda v, k=kind: codec.encode(k, v.encode()))
        op.drop_column(table, column)
        op.alter_column(table, tmp, new_column_name=column, nullable=nullable, comment=COMMENT)
        # Đã nén sẵn: Postgres không nén lại (và cắt được phần đầu giá trị mà không đọc hết).
        op.execute(f"ALTER TABLE {table} ALTER COLUMN {column} SET STORAGE EXTERNAL")


def downgrade() -> None:
    codec = Codec()
    load_dictionaries_sync(op.get_bind(), codec)
    for table, column, _kind, nullable, old_type, _source in reversed(COLUMNS):
        tmp = f"{column}_old"
        op.add_column(table, sa.Column(tmp, old_type, nullable=True))
        # Chuỗi JSON ghi thẳng vào cột jsonb (asyncpg nhận jsonb dạng chuỗi).
        _convert(table, column, tmp, lambda v: codec.decode(v).decode())
        op.drop_column(table, column)
        op.alter_column(table, tmp, new_column_name=column, nullable=nullable)
    op.alter_column(
        "job_postings", "raw", server_default=sa.text("'{}'::jsonb"), existing_nullable=False
    )

    op.drop_index("ix_source_fetches_fetched_at", table_name="source_fetches")
    op.drop_index("ix_company_runs_started_at", table_name="company_runs")
    op.execute("ALTER TABLE failed_pages ALTER COLUMN html_zstd SET STORAGE EXTENDED")
    op.alter_column("failed_pages", "html_zstd", comment=None)
    op.drop_column("failed_pages", "http_status")
    op.drop_column("failed_pages", "run_id")
    op.drop_column("companies", "note")
    op.drop_column("companies", "input_website")
    op.drop_table("compression_dicts")
