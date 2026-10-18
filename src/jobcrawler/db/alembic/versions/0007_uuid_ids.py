"""ID dạng UUID (v7) thay bigint tự tăng.

- Khoá chính bigint IDENTITY của companies, company_aliases, source_profiles, job_postings,
  board_index_runs, jobs, job_reviews, source_fetches, logs, failed_pages và mọi cột tham chiếu
  tới chúng chuyển sang `uuid`. `compression_dicts.id` giữ nguyên: đó là id từ điển zstd ghi trong
  4 byte đầu mỗi giá trị nén, không phải id dòng.
- ID mới là UUIDv7, sinh ở ứng dụng (`db/ids.uuid7`); mặc định phía DB `uuid_v7()` (cùng định
  dạng) cho insert bằng SQL thô / COPY.
- Dòng cũ đổi bằng hàm tất định `legacy_uuid(mã bảng, id)`: UUIDv7 với mốc thời gian
  2026-01-01 + id (ms) và mã bảng trong phần ngẫu nhiên. Khoá ngoại đổi bằng cùng hàm nên không
  cần join; thứ tự id cũ giữ nguyên và nằm trước mọi id mới. `ALTER COLUMN ... TYPE ... USING`
  tự dựng lại index / ràng buộc duy nhất chứa cột; khoá ngoại bỏ trước, tạo lại sau.
- Downgrade: id cũ đảo ngược về đúng số ban đầu, id sinh sau migration đánh số tiếp theo thứ tự
  UUID (bảng ánh xạ tạm); quan hệ giữ nguyên.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-18 09:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Bảng có khoá chính `id` bigint → mã bảng dùng trong legacy_uuid (không đổi số đã dùng).
TABLES: dict[str, int] = {
    "companies": 1,
    "company_aliases": 2,
    "source_profiles": 3,
    "job_postings": 4,
    "board_index_runs": 5,
    "jobs": 6,
    "job_reviews": 7,
    "source_fetches": 8,
    "logs": 9,
    "failed_pages": 10,
}

# (bảng, cột, bảng được tham chiếu, ON DELETE; None = không có khoá ngoại).
REFS: list[tuple[str, str, str, str | None]] = [
    ("company_aliases", "company_id", "companies", "CASCADE"),
    ("source_profiles", "company_id", "companies", "SET NULL"),
    ("job_postings", "source_profile_id", "source_profiles", "SET NULL"),
    ("job_postings", "job_id", "jobs", "SET NULL"),
    ("job_postings", "company_id", "companies", "SET NULL"),
    ("jobs", "company_id", "companies", "CASCADE"),
    ("jobs", "canonical_posting_id", "job_postings", "SET NULL"),
    ("job_reviews", "job_id", "jobs", "CASCADE"),
    ("company_runs", "company_id", "companies", "CASCADE"),
    ("source_fetches", "company_id", "companies", "CASCADE"),
    ("logs", "company_id", "companies", None),
]

# Như hàm uuidv7() có sẵn từ Postgres 18: lấy UUIDv4 ngẫu nhiên, ghi đè 48 bit đầu bằng thời gian
# (ms) và đổi version 4 → 7 (bật bit 52, 53).
UUID_V7 = """
CREATE OR REPLACE FUNCTION uuid_v7() RETURNS uuid LANGUAGE sql VOLATILE AS $$
  SELECT encode(
    set_bit(set_bit(
      overlay(uuid_send(gen_random_uuid())
              PLACING substring(int8send(floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint)
                                FROM 3)
              FROM 1 FOR 6),
      52, 1), 53, 1),
    'hex')::uuid
$$
"""

LEGACY_UUID = """
CREATE FUNCTION legacy_uuid(tag int, id bigint) RETURNS uuid
LANGUAGE sql IMMUTABLE STRICT AS $$
  SELECT (lpad(to_hex(1767225600000 + id), 12, '0') || '7000' || '8'
          || lpad(to_hex(tag), 3, '0') || '000000000000')::uuid
$$
"""

# Ngược của LEGACY_UUID; NULL nếu không phải dạng đó (id sinh sau migration).
LEGACY_ID = """
CREATE FUNCTION legacy_id(tag int, u uuid) RETURNS bigint
LANGUAGE sql IMMUTABLE STRICT AS $$
  SELECT CASE
    WHEN substr(h, 13, 8) = '70008' || lpad(to_hex(tag), 3, '0')
         AND substr(h, 21) = '000000000000'
    THEN ('x' || lpad(substr(h, 1, 12), 16, '0'))::bit(64)::bigint - 1767225600000
  END
  FROM (SELECT replace(u::text, '-', '') AS h) AS hex
$$
"""


def _fk_name(table: str, column: str, referred: str) -> str:
    return f"fk_{table}_{column}_{referred}"


def _drop_fks() -> None:
    for table, column, referred, ondelete in REFS:
        if ondelete is not None:
            op.drop_constraint(_fk_name(table, column, referred), table, type_="foreignkey")


def _create_fks() -> None:
    for table, column, referred, ondelete in REFS:
        if ondelete is not None:
            op.create_foreign_key(
                _fk_name(table, column, referred),
                table,
                referred,
                [column],
                ["id"],
                ondelete=ondelete,
            )


def _alter(table: str, clauses: list[str]) -> None:
    # Một lệnh cho mọi cột của bảng: bảng chỉ bị ghi lại một lần.
    op.execute(f"ALTER TABLE {table} " + ", ".join(clauses))


def upgrade() -> None:
    op.execute(UUID_V7)
    op.execute(LEGACY_UUID)
    _drop_fks()
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN id DROP IDENTITY")
    for table in [*TABLES, "company_runs"]:
        clauses = []
        if table in TABLES:
            clauses += [
                f"ALTER COLUMN id TYPE uuid USING legacy_uuid({TABLES[table]}, id)",
                "ALTER COLUMN id SET DEFAULT uuid_v7()",
            ]
        for ref_table, column, referred, _ in REFS:
            if ref_table == table:
                clauses.append(
                    f"ALTER COLUMN {column} TYPE uuid USING legacy_uuid({TABLES[referred]}, {column})"
                )
        _alter(table, clauses)
    _create_fks()
    op.execute("DROP FUNCTION legacy_uuid(int, bigint)")


def downgrade() -> None:
    # Id cũ (dạng legacy_uuid) đảo ngược đúng số ban đầu; id mới đánh số tiếp sau theo thứ tự UUID.
    op.execute(LEGACY_ID)
    op.execute("CREATE TABLE _id_map (tbl text, u uuid, n bigint, PRIMARY KEY (tbl, u))")
    for table, tag in TABLES.items():
        op.execute(
            f"INSERT INTO _id_map SELECT '{table}', id,"
            f" (SELECT coalesce(max(legacy_id({tag}, id)), 0) FROM {table})"
            f" + row_number() OVER (ORDER BY id)"
            f" FROM {table} WHERE legacy_id({tag}, id) IS NULL"
        )
    op.execute(
        "CREATE FUNCTION _to_bigint(t text, tag int, v uuid) RETURNS bigint"
        " LANGUAGE sql STABLE STRICT AS $$"
        " SELECT coalesce(legacy_id(tag, v), (SELECT n FROM _id_map WHERE tbl = t AND u = v)) $$"
    )
    _drop_fks()
    for table in [*TABLES, "company_runs"]:
        clauses = []
        if table in TABLES:
            clauses += [
                "ALTER COLUMN id DROP DEFAULT",
                f"ALTER COLUMN id TYPE bigint USING _to_bigint('{table}', {TABLES[table]}, id)",
            ]
        for ref_table, column, referred, _ in REFS:
            if ref_table == table:
                clauses.append(
                    f"ALTER COLUMN {column} TYPE bigint"
                    f" USING _to_bigint('{referred}', {TABLES[referred]}, {column})"
                )
        _alter(table, clauses)
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY")
        op.execute(
            f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), coalesce(max(id), 0) + 1,"
            f" false) FROM {table}"
        )
    _create_fks()
    op.execute("DROP FUNCTION _to_bigint(text, int, uuid)")
    op.execute("DROP TABLE _id_map")
    op.execute("DROP FUNCTION legacy_id(int, uuid)")
    op.execute("DROP FUNCTION uuid_v7()")
