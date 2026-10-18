from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from jobcrawler.config import Settings


def _json(value: Any) -> str:
    # Cột JSONB: id (UUID), datetime... ghi dạng chuỗi thay vì lỗi "not JSON serializable".
    return json.dumps(value, ensure_ascii=False, default=str)


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        json_serializer=_json,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
