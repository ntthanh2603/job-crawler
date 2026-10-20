"""Chạy Alembic từ code để `jobcrawler db ...` dùng được ở mọi thư mục và trong container."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from jobcrawler.common.config import get_settings

ALEMBIC_DIR = Path(__file__).with_name("alembic")


def alembic_config(database_url: str | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("file_template", "%%(rev)s_%%(slug)s")
    url = database_url or get_settings().database_url
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return cfg


def upgrade(revision: str = "head", *, database_url: str | None = None) -> None:
    command.upgrade(alembic_config(database_url), revision)


def downgrade(revision: str, *, database_url: str | None = None) -> None:
    command.downgrade(alembic_config(database_url), revision)


def current(*, database_url: str | None = None) -> None:
    command.current(alembic_config(database_url), verbose=True)


def check(*, database_url: str | None = None) -> None:
    """Ném lỗi nếu model và schema DB lệch nhau."""
    command.check(alembic_config(database_url))


def revision(message: str, *, autogenerate: bool = True) -> None:
    """Tạo migration mới, đánh số tuần tự 0001, 0002, ..."""
    cfg = alembic_config()
    head = ScriptDirectory.from_config(cfg).get_current_head()
    next_id = f"{int(head) + 1:04d}" if head and head.isdigit() else None
    command.revision(cfg, message=message, autogenerate=autogenerate, rev_id=next_id)
