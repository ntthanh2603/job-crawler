"""Quản lý hệ thống ngoài (`api_clients`) và khoá API (`api_keys`) cho trang Tích hợp."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jobcrawler.common.pagination import GetManyQuery, Page, ordering, paginate
from jobcrawler.database.models import ApiClient, ApiKey, CompanyRef
from jobcrawler.modules.integration import feed_service, key_service, webhook_service


class ApiClientQuery(GetManyQuery):
    sortBy: Literal["created_at", "name"] = "created_at"
    limit: int = Field(default=50, ge=1, le=100)


@dataclass(slots=True)
class ClientSummary:
    client: ApiClient
    companies: int
    active_keys: int
    last_used_at: datetime | None


def _summary_stmt():
    refs = (
        select(func.count())
        .where(CompanyRef.client_id == ApiClient.id)
        .correlate(ApiClient)
        .scalar_subquery()
    )
    keys = (
        select(func.count())
        .where(ApiKey.client_id == ApiClient.id, ApiKey.revoked_at.is_(None))
        .correlate(ApiClient)
        .scalar_subquery()
    )
    used = (
        select(func.max(ApiKey.last_used_at))
        .where(ApiKey.client_id == ApiClient.id)
        .correlate(ApiClient)
        .scalar_subquery()
    )
    return select(ApiClient, refs, keys, used)


async def list_clients(session: AsyncSession, query: ApiClientQuery) -> Page[ClientSummary]:
    stmt = _summary_stmt()
    if query.search.strip():
        stmt = stmt.where(ApiClient.name.ilike(f"%{query.search.strip()}%"))
    key = ApiClient.created_at if query.sortBy == "created_at" else ApiClient.name
    return await paginate(
        session,
        stmt,
        query.page,
        limit=query.limit,
        order_by=ordering(key, query.sortOrder, ApiClient.id),
        scalars=False,
        convert=lambda row: ClientSummary(*row),
    )


async def get_client(
    session: AsyncSession, client_id: UUID
) -> tuple[ClientSummary, list[ApiKey]] | None:
    row = (await session.execute(_summary_stmt().where(ApiClient.id == client_id))).first()
    if row is None:
        return None
    keys = list(
        await session.scalars(
            select(ApiKey)
            .where(ApiKey.client_id == client_id)
            .order_by(ApiKey.revoked_at.is_not(None), ApiKey.created_at.desc())
            .limit(100)
        )
    )
    return ClientSummary(*row), keys


async def name_taken(session: AsyncSession, name: str, *, other_than: UUID | None = None) -> bool:
    stmt = select(ApiClient.id).where(func.lower(ApiClient.name) == name.strip().lower())
    if other_than is not None:
        stmt = stmt.where(ApiClient.id != other_than)
    return await session.scalar(stmt) is not None


async def create_client(
    session: AsyncSession,
    *,
    name: str,
    scopes: list[str],
    all_companies: bool,
    include_description: bool,
    created_by: str,
) -> ApiClient:
    client = ApiClient(
        name=name.strip(),
        scopes=sorted(set(scopes)),
        all_companies=all_companies,
        include_description=include_description,
        created_by=created_by,
    )
    session.add(client)
    await session.flush()
    return client


class WebhookConfigError(ValueError):
    """Cấu hình webhook không dùng được (URL sai, bật khi chưa có URL / secret)."""


def clean_webhook_url(value: str) -> str | None:
    """URL webhook: rỗng = xoá; phải là http(s)://host..."""
    value = value.strip()
    if not value:
        return None
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname or " " in value:
        raise WebhookConfigError("URL webhook phải bắt đầu bằng http:// hoặc https://")
    return value


async def update_client(
    session: AsyncSession, client: ApiClient, values: dict[str, object]
) -> dict[str, object]:
    """Ghi các trường có trong `values`; trả {trường: {before, after}} của trường đổi."""
    changes: dict[str, object] = {}
    values = dict(values)
    if "webhook_url" in values:
        url = clean_webhook_url(str(values.pop("webhook_url") or ""))
        before = webhook_service.unseal(webhook_service.URL_FIELD, client.webhook_url)
        if url != before:
            client.webhook_url = webhook_service.seal(webhook_service.URL_FIELD, url)
            changes["webhook_url"] = {
                "before": webhook_service.url_hint(before),
                "after": webhook_service.url_hint(url),
            }
            if url is None:
                values["webhook_enabled"] = False
    if values.get("webhook_enabled") and not client.webhook_enabled:
        if client.webhook_url is None or client.webhook_secret is None:
            raise WebhookConfigError("Cần URL và secret webhook trước khi bật")
        await webhook_service.start_cursor(session, client)
    for field_name, value in values.items():
        if field_name == "name" and isinstance(value, str):
            value = value.strip()
        if field_name == "scopes" and isinstance(value, list):
            value = sorted(set(value))
        before = getattr(client, field_name)
        if before != value:
            changes[field_name] = {"before": before, "after": value}
            setattr(client, field_name, value)
    if changes:
        client.updated_at = datetime.now(UTC)
    return changes


async def delete_client(session: AsyncSession, client_id: UUID) -> bool:
    """Xoá client cùng khoá và mã doanh nghiệp của nó (doanh nghiệp vẫn giữ)."""
    result = await session.execute(delete(ApiClient).where(ApiClient.id == client_id))
    return bool(getattr(result, "rowcount", 0))


async def rotate_secret(session: AsyncSession, client: ApiClient) -> str:
    """Secret ký webhook mới (cũ hết dùng ngay; lô đang lỗi được ký lại bằng secret mới)."""
    secret = webhook_service.new_secret()
    client.webhook_secret = webhook_service.seal(webhook_service.SECRET_FIELD, secret)
    client.updated_at = datetime.now(UTC)
    return secret


async def head(session: AsyncSession) -> int:
    return await feed_service.head(session)


async def create_key(
    session: AsyncSession, client_id: UUID, *, created_by: str
) -> tuple[ApiKey, str]:
    return await key_service.create_key(session, client_id, created_by=created_by)


async def revoke_key(session: AsyncSession, client_id: UUID, key_id: UUID) -> bool:
    result = await session.execute(
        update(ApiKey)
        .where(ApiKey.id == key_id, ApiKey.client_id == client_id, ApiKey.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    return bool(getattr(result, "rowcount", 0))
