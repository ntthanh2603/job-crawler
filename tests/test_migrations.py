from __future__ import annotations

from alembic.script import ScriptDirectory

from jobcrawler.db.migrate import alembic_config
from jobcrawler.db.models import Base


def _script() -> ScriptDirectory:
    return ScriptDirectory.from_config(alembic_config("postgresql+asyncpg://u:p@h/db"))


def test_single_linear_head() -> None:
    script = _script()
    assert script.get_heads() == ["0007"]
    assert [r.revision for r in script.walk_revisions()] == [
        "0007",
        "0006",
        "0005",
        "0004",
        "0003",
        "0002",
        "0001",
    ]


def test_migrations_create_every_model_table() -> None:
    source = ""
    for revision in _script().walk_revisions():
        assert revision.path is not None
        with open(revision.path, encoding="utf-8") as f:
            source += f.read()
    compact = " ".join(source.split())
    for table in Base.metadata.tables:
        assert f'op.create_table( "{table}"' in compact, table
    for ext in ("pg_trgm", "unaccent", "vector"):
        assert ext in source
