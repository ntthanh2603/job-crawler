from __future__ import annotations

from alembic.script import ScriptDirectory

from jobcrawler.db.migrate import alembic_config
from jobcrawler.db.models import Base


def test_single_linear_head() -> None:
    script = ScriptDirectory.from_config(alembic_config("postgresql+asyncpg://u:p@h/db"))
    assert script.get_heads() == ["0001"]


def test_initial_migration_creates_every_model_table() -> None:
    script = ScriptDirectory.from_config(alembic_config("postgresql+asyncpg://u:p@h/db"))
    revision = script.get_revision("0001")
    assert revision is not None
    assert revision.path is not None
    with open(revision.path, encoding="utf-8") as f:
        source = f.read()
    for table in Base.metadata.tables:
        assert f'op.create_table(\n        "{table}"' in source, table
    for ext in ("pg_trgm", "unaccent", "vector"):
        assert ext in source
