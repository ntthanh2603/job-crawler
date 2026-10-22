"""API quản lý hệ thống ngoài cho trang Tích hợp (`/api/api-clients`, đăng nhập giao diện): tạo
client, tạo / thu hồi khoá API (khoá chỉ hiện một lần), phạm vi đọc. Hệ thống ngoài gọi API ở
`modules/integration/integration_router.py` bằng các khoá này."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request

from jobcrawler.common.api import OkOut, PageOut, actor_of, page_out, res_of
from jobcrawler.database.models import ApiClient, ApiKey
from jobcrawler.modules.api_clients import api_clients_service
from jobcrawler.modules.api_clients.api_clients_schema import (
    ApiClientDetailOut,
    ApiClientIn,
    ApiClientOut,
    ApiClientUpdateIn,
    ApiKeyCreatedOut,
    ApiKeyOut,
)
from jobcrawler.modules.audit import audit_service

router = APIRouter(prefix="/api", tags=["api-clients"])


def _client_out(s: api_clients_service.ClientSummary) -> ApiClientOut:
    c = s.client
    return ApiClientOut(
        id=c.id,
        name=c.name,
        scopes=list(c.scopes or []),
        all_companies=c.all_companies,
        include_description=c.include_description,
        companies=s.companies,
        active_keys=s.active_keys,
        last_used_at=s.last_used_at,
        created_by=c.created_by,
        created_at=c.created_at,
    )


def _key_out(k: ApiKey) -> ApiKeyOut:
    return ApiKeyOut(
        id=k.id,
        prefix=k.prefix,
        created_by=k.created_by,
        created_at=k.created_at,
        last_used_at=k.last_used_at,
        revoked_at=k.revoked_at,
    )


def _forget_keys(request: Request) -> None:
    """Thu hồi / đổi phạm vi có hiệu lực ngay ở process này (process API khác: sau ≤ 10 s)."""
    auth = getattr(request.app.state, "api_keys", None)
    if auth is not None:
        auth.forget()


async def _detail(request: Request, client_id: UUID) -> ApiClientDetailOut:
    async with res_of(request).sessionmaker() as session:
        found = await api_clients_service.get_client(session, client_id)
    if found is None:
        raise HTTPException(404, "Không có hệ thống này")
    summary, keys = found
    return ApiClientDetailOut(**_client_out(summary).model_dump(), keys=[_key_out(k) for k in keys])


@router.get("/api-clients")
async def get_api_clients(
    request: Request, query: Annotated[api_clients_service.ApiClientQuery, Query()]
) -> PageOut[ApiClientOut]:
    """Các hệ thống ngoài dùng API tích hợp."""
    async with res_of(request).sessionmaker() as session:
        page = await api_clients_service.list_clients(session, query)
    return page_out(page, [_client_out(s) for s in page.items])


@router.post("/api-clients")
async def post_api_client(request: Request, body: ApiClientIn) -> ApiClientDetailOut:
    res = res_of(request)
    async with res.sessionmaker.begin() as session:
        if await api_clients_service.name_taken(session, body.name):
            raise HTTPException(409, "Đã có hệ thống tên này")
        client = await api_clients_service.create_client(
            session,
            name=body.name,
            scopes=list(body.scopes),
            all_companies=body.all_companies,
            include_description=body.include_description,
            created_by=actor_of(request),
        )
        client_id = client.id
    await audit_service.record_safely(
        res.sessionmaker,
        action=audit_service.API_CLIENT_CREATE,
        actor=actor_of(request),
        resource_type="api_client",
        resource_id=client_id,
        metadata={"name": body.name.strip(), "scopes": sorted(set(body.scopes))},
    )
    return await _detail(request, client_id)


@router.get("/api-clients/{client_id}")
async def get_api_client(request: Request, client_id: UUID) -> ApiClientDetailOut:
    return await _detail(request, client_id)


@router.patch("/api-clients/{client_id}")
async def patch_api_client(
    request: Request, client_id: UUID, body: ApiClientUpdateIn
) -> ApiClientDetailOut:
    res = res_of(request)
    values = body.model_dump(exclude_unset=True, exclude_none=True)
    async with res.sessionmaker.begin() as session:
        client = await session.get(ApiClient, client_id, with_for_update=True)
        if client is None:
            raise HTTPException(404, "Không có hệ thống này")
        name = values.get("name")
        if isinstance(name, str) and await api_clients_service.name_taken(
            session, name, other_than=client_id
        ):
            raise HTTPException(409, "Đã có hệ thống tên này")
        changes = await api_clients_service.update_client(session, client, values)
    if changes:
        _forget_keys(request)
        await audit_service.record_safely(
            res.sessionmaker,
            action=audit_service.API_CLIENT_UPDATE,
            actor=actor_of(request),
            resource_type="api_client",
            resource_id=client_id,
            changes=changes,
        )
    return await _detail(request, client_id)


@router.delete("/api-clients/{client_id}")
async def delete_api_client(request: Request, client_id: UUID) -> OkOut:
    """Xoá hệ thống cùng mọi khoá và mã doanh nghiệp của nó; doanh nghiệp và việc làm vẫn giữ."""
    res = res_of(request)
    async with res.sessionmaker.begin() as session:
        client = await session.get(ApiClient, client_id)
        name = client.name if client else None
        deleted = await api_clients_service.delete_client(session, client_id)
    if not deleted:
        raise HTTPException(404, "Không có hệ thống này")
    _forget_keys(request)
    await audit_service.record_safely(
        res.sessionmaker,
        action=audit_service.API_CLIENT_DELETE,
        actor=actor_of(request),
        resource_type="api_client",
        resource_id=client_id,
        metadata={"name": name},
    )
    return OkOut(ok=True)


@router.post("/api-clients/{client_id}/keys")
async def post_api_client_key(request: Request, client_id: UUID) -> ApiKeyCreatedOut:
    """Khoá mới (chỉ hiện một lần). Khoá cũ vẫn dùng được tới khi thu hồi: đổi khoá không gián
    đoạn."""
    res = res_of(request)
    async with res.sessionmaker.begin() as session:
        if await session.get(ApiClient, client_id) is None:
            raise HTTPException(404, "Không có hệ thống này")
        row, key = await api_clients_service.create_key(
            session, client_id, created_by=actor_of(request)
        )
        out = ApiKeyCreatedOut(id=row.id, prefix=row.prefix, key=key, created_at=row.created_at)
    await audit_service.record_safely(
        res.sessionmaker,
        action=audit_service.API_KEY_CREATE,
        actor=actor_of(request),
        resource_type="api_client",
        resource_id=client_id,
        metadata={"key_id": str(out.id), "prefix": out.prefix},
    )
    return out


@router.delete("/api-clients/{client_id}/keys/{key_id}")
async def delete_api_client_key(request: Request, client_id: UUID, key_id: UUID) -> OkOut:
    """Thu hồi khoá: hệ thống ngoài nhận 401 với khoá này (process API khác sau ≤ 10 s)."""
    res = res_of(request)
    async with res.sessionmaker.begin() as session:
        revoked = await api_clients_service.revoke_key(session, client_id, key_id)
    if not revoked:
        raise HTTPException(404, "Không có khoá này (hoặc đã thu hồi)")
    _forget_keys(request)
    await audit_service.record_safely(
        res.sessionmaker,
        action=audit_service.API_KEY_REVOKE,
        actor=actor_of(request),
        resource_type="api_client",
        resource_id=client_id,
        metadata={"key_id": str(key_id)},
    )
    return OkOut(ok=True)
