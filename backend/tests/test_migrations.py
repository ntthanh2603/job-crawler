from __future__ import annotations

from alembic.script import ScriptDirectory

from jobcrawler.database.migrate import alembic_config
from jobcrawler.database.models import JOB_SALARY_MONTH_SQL, Base


def _script() -> ScriptDirectory:
    return ScriptDirectory.from_config(alembic_config("postgresql+asyncpg://u:p@h/db"))


def test_single_linear_head() -> None:
    script = _script()
    assert script.get_heads() == ["0006"]
    # walk_revisions đi từ head về gốc: chuỗi thẳng 0006 → 0005 → … → 0001.
    assert [r.revision for r in script.walk_revisions()] == [
        "0006",
        "0005",
        "0004",
        "0003",
        "0002",
        "0001",
    ]


def test_salary_index_matches_model_expression() -> None:
    """Lọc / sắp theo lương chỉ dùng được index khi biểu thức y hệt chuỗi trong revision."""
    revision = _script().get_revision("0006")
    assert revision is not None
    assert revision.module.SALARY_MONTH == JOB_SALARY_MONTH_SQL


def test_migrations_create_every_model_table() -> None:
    source = ""
    for revision in _script().walk_revisions():
        assert revision.path is not None
        with open(revision.path, encoding="utf-8") as f:
            source += f.read()
    compact = " ".join(source.split())
    for table in Base.metadata.tables:
        assert f'op.create_table( "{table}"' in compact, table
    assert "CREATE OR REPLACE FUNCTION uuid_v7()" in source
    for ext in ("pg_trgm", "unaccent"):
        assert ext in source
