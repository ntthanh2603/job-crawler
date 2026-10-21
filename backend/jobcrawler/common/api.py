"""Phần chung của các router API JSON (`modules/*/*_router.py`): gốc mô hình trả về `ApiOut`,
trang `PageOut` (như open-asm; tham số danh sách `GetManyQuery` ở `common/pagination.py`),
`known_values`, lấy tài nguyên / cài đặt / người thao tác của request.

Mỗi endpoint trả một mô hình Pydantic (`...Out`): spec `/api/openapi.json` (bản commit ở
`backend/openapi.json`, `scripts/export_openapi.py`) mô tả đúng JSON trả về, kiểu TypeScript của
giao diện sinh từ spec đó."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Annotated, Any

from fastapi import Request
from pydantic import BaseModel, WithJsonSchema

from jobcrawler.common import pagination
from jobcrawler.common.config import Settings
from jobcrawler.common.schemas import API_OUTPUT
from jobcrawler.modules.audit import audit_service
from jobcrawler.services.resources import Resources


def res_of(request: Request) -> Resources:
    return request.app.state.res


def settings_of(request: Request) -> Settings:
    """Settings gốc của process API (chưa đè cài đặt từ giao diện)."""
    return request.app.state.settings


def user_of(request: Request) -> str | None:
    return getattr(request.state, "user", None)


def actor_of(request: Request) -> str:
    """Người thao tác ghi vào nhật ký (`modules/audit/audit_service.py`)."""
    return audit_service.actor_of(user_of(request))


class ApiOut(BaseModel):
    """Gốc của mọi mô hình trả về: trường có mặc định vẫn luôn có trong JSON, nên spec ghi là bắt
    buộc (kiểu sinh cho giao diện không thành `x?:`)."""

    model_config = API_OUTPUT


# Trần của tham số số giờ (cửa sổ thống kê, `stale_hours`): 10 năm; lớn hơn thì `now - timedelta`
# tràn (OverflowError → 500).
MAX_HOURS = 10 * 366 * 24

# Mốc thời gian: `datetime` (ISO 8601), hoặc chuỗi đã có sẵn trong JSON cache của trang tổng quan
# (`modules/overview/overview_service.py` ghi bằng `str()`) — giữ nguyên chuỗi đó. Spec chỉ ghi
# `date-time`.
Timestamp = Annotated[datetime | str, WithJsonSchema({"type": "string", "format": "date-time"})]


def known_values(values: Iterable[str], *, nullable: bool = False) -> WithJsonSchema:
    """Spec liệt kê `values` cho một trường chuỗi đọc từ Redis / JSON (không có ràng buộc ở DB như
    các cột enum): khi chạy không kiểm, giá trị lạ (job cũ trong queue...) vẫn trả ra nguyên văn."""
    schema: dict[str, Any] = {"type": "string", "enum": [str(v) for v in values]}
    return WithJsonSchema({"anyOf": [schema, {"type": "null"}]} if nullable else schema)


class PageOut[T](ApiOut):
    """Một trang danh sách (như `GetManyBaseResponseDto` của open-asm)."""

    data: list[T]
    total: int  # đếm tối đa pagination.COUNT_CAP
    page: int
    limit: int
    pageCount: int
    hasNextPage: bool
    exact: bool = True  # False: thực tế nhiều hơn `total` (server chỉ đếm tới COUNT_CAP)


def page_out[T](page: pagination.Page[Any], items: list[T]) -> PageOut[T]:
    return PageOut(
        data=items,
        total=page.total,
        page=page.page,
        limit=page.limit,
        pageCount=page.page_count,
        hasNextPage=page.has_next_page,
        exact=page.exact,
    )


class QueuedOut(ApiOut):
    queued: bool  # False: đã có job cùng khoá đang chờ / chạy


class OkOut(ApiOut):
    ok: bool
