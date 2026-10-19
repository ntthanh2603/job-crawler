"""API trang Cài đặt và trang Workers (`/api/settings`, `/api/workers`).

Cài đặt: giá trị gốc là `app.state.settings` (biến môi trường / .env lúc API khởi động), đè bởi
bảng `app_settings` (`services.app_settings`). Workers: đọc danh sách worker lấy trang trong Redis
(`gateway.registry`), yêu cầu worker dừng nhận việc / nhận việc lại.
"""

from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, WithJsonSchema

from jobcrawler.config import Settings
from jobcrawler.gateway.registry import (
    Keys,
    WorkerStatus,
    is_draining,
    list_workers,
    load_worker,
    request_drain,
)
from jobcrawler.logging import get_logger
from jobcrawler.resources import Resources
from jobcrawler.services import app_settings, audit
from jobcrawler.web.api import ApiOut

router = APIRouter(prefix="/api", tags=["settings"])

log = get_logger(__name__)


def _res(request: Request) -> Resources:
    return request.app.state.res


def _base(request: Request) -> Settings:
    """Settings gốc của process API (chưa đè cài đặt từ giao diện)."""
    return request.app.state.settings


def _user(request: Request) -> str:
    """Người thao tác (tên đăng nhập do `GuardMiddleware` gắn, không có thì `anonymous`): ghi
    vào `app_settings.updated_by` và nhật ký thao tác."""
    return audit.actor_of(getattr(request.state, "user", None))


# --- cài đặt -------------------------------------------------------------------------------------


class SettingsUpdate(BaseModel):
    # Tên cài đặt → giá trị mới; `null` = về giá trị biến môi trường / mặc định.
    values: dict[str, Any]


# Giá trị một cài đặt như API trả (`app_settings._display`: JSON của field, bí mật luôn null, proxy
# đã ẩn mật khẩu). Khi chạy không kiểm lại; spec liệt kê các dạng có thể có.
type SettingValue = Annotated[
    Any,
    WithJsonSchema(
        {
            "anyOf": [
                {"type": "string"},
                {"type": "number"},
                {"type": "boolean"},
                {"type": "array", "items": {"type": "string"}},
                {
                    "type": "object",
                    "additionalProperties": {"anyOf": [{"type": "string"}, {"type": "number"}]},
                },
                {"type": "null"},
            ]
        }
    ),
]


class SettingOptionOut(ApiOut):
    value: str
    label: str
    help: str | None


class SettingSectionOut(ApiOut):
    """Mục con trong nhóm: tiêu đề, giải thích ngắn, các cài đặt theo thứ tự hiển thị."""

    title: str
    description: str
    keys: list[str]


class SettingItemOut(ApiOut):
    key: str
    label: str
    help: str
    kind: app_settings.Kind
    options: list[SettingOptionOut]
    unit: str | None
    min: float | None
    max: float | None
    min_exclusive: bool
    max_exclusive: bool
    nullable: bool  # để trống được (null)
    placeholder: str | None
    value_type: str | None  # kiểu giá trị của bảng `dict` ("str" | "float")
    live: bool  # False: chỉ có hiệu lực sau khi khởi động lại service
    value: SettingValue  # bí mật luôn null (xem `is_set`)
    default: SettingValue
    env_value: SettingValue
    source: app_settings.Source  # giá trị đang dùng lấy từ đâu
    secret: bool
    is_set: bool | None  # chỉ với bí mật
    env_is_set: bool | None


class SettingGroupOut(ApiOut):
    group: str
    title: str
    description: str
    sections: list[SettingSectionOut]
    items: list[SettingItemOut]


class SettingsPageOut(ApiOut):
    version: int  # tăng mỗi lần lưu; 0 khi chưa lưu lần nào
    groups: list[SettingGroupOut]


class SettingsSavedOut(SettingsPageOut):
    restart_required: list[str]  # cài đặt vừa đổi chỉ có hiệu lực sau khi khởi động lại


async def _settings_page(request: Request) -> SettingsPageOut:
    res, base = _res(request), _base(request)
    overrides = await app_settings.load_overrides(res.sessionmaker)
    effective = app_settings.apply_overrides(base, overrides)
    return SettingsPageOut.model_validate(
        {
            "version": await app_settings.settings_version(res.redis, base.redis_prefix),
            "groups": app_settings.describe(base, overrides, effective),
        }
    )


async def _save(request: Request, values: dict[str, Any]) -> SettingsSavedOut:
    res, base = _res(request), _base(request)
    before = await app_settings.load_effective(res.sessionmaker, base)
    try:
        after = await app_settings.save(
            res.sessionmaker, res.redis, base.redis_prefix, base, values, user=_user(request)
        )
    except app_settings.SettingsError as exc:
        raise HTTPException(422, detail={"errors": exc.errors}) from exc
    page = await _settings_page(request)
    return SettingsSavedOut(
        version=page.version,
        groups=page.groups,
        restart_required=app_settings.restart_required(before, after, values),
    )


@router.get("/settings")
async def get_settings(request: Request) -> SettingsPageOut:
    """Mọi cài đặt sửa được, theo nhóm: giá trị hiệu lực, mặc định, từ biến môi trường, nguồn."""
    return await _settings_page(request)


@router.put("/settings")
async def put_settings(request: Request, body: SettingsUpdate) -> SettingsSavedOut:
    """Lưu nhiều cài đặt cùng lúc (kiểm tra tất cả trước, lỗi thì không lưu gì: 422 kèm
    `detail.errors` = tên cài đặt → thông báo). `restart_required`: các cài đặt vừa đổi chỉ có
    hiệu lực sau khi khởi động lại service."""
    return await _save(request, body.values)


@router.delete("/settings/{key}")
async def reset_setting(request: Request, key: str) -> SettingsSavedOut:
    """Bỏ giá trị đặt ở giao diện, về giá trị biến môi trường / mặc định."""
    if key not in app_settings.SPEC_BY_KEY:
        raise HTTPException(404, "Không có cài đặt này hoặc cài đặt chỉ đặt bằng biến môi trường")
    return await _save(request, {key: None})


# --- worker lấy trang ----------------------------------------------------------------------------


class WorkerPoolOut(ApiOut):
    """Cộng dồn các worker đang sống của một pool."""

    workers: int
    http_slots: int
    render_slots: int
    http_inflight: int
    render_inflight: int


class WorkersSummaryOut(ApiOut):
    alive: int
    gone: int
    draining: int
    pools: dict[str, WorkerPoolOut]
    http_slots: int
    render_slots: int
    http_inflight: int
    render_inflight: int
    done: int  # cả worker vừa rời
    failed: int


class WorkersPageOut(ApiOut):
    workers: list[WorkerStatus]  # đang sống trước, kèm worker vừa rời
    summary: WorkersSummaryOut
    now_ms: int  # giờ máy chủ (ms epoch) lúc trả lời, để tính "lần cuối báo" không lệch đồng hồ


class DrainOut(ApiOut):
    worker_id: str
    draining: bool


def _summary(workers: list[WorkerStatus]) -> WorkersSummaryOut:
    alive = [w for w in workers if w.alive]
    pools: dict[str, WorkerPoolOut] = {}
    for w in alive:
        pool = pools.setdefault(
            w.pool,
            WorkerPoolOut(
                workers=0, http_slots=0, render_slots=0, http_inflight=0, render_inflight=0
            ),
        )
        pool.workers += 1
        pool.http_slots += w.http_slots
        pool.render_slots += w.render_slots
        pool.http_inflight += w.stats.http_inflight
        pool.render_inflight += w.stats.render_inflight
    return WorkersSummaryOut(
        alive=len(alive),
        gone=len(workers) - len(alive),
        draining=sum(1 for w in alive if w.draining),
        pools=pools,
        http_slots=sum(p.http_slots for p in pools.values()),
        render_slots=sum(p.render_slots for p in pools.values()),
        http_inflight=sum(p.http_inflight for p in pools.values()),
        render_inflight=sum(p.render_inflight for p in pools.values()),
        done=sum(w.stats.http_done + w.stats.render_done for w in workers),
        failed=sum(w.stats.http_failed + w.stats.render_failed for w in workers),
    )


@router.get("/workers")
async def get_workers(request: Request) -> WorkersPageOut:
    """Worker lấy trang đã biết (đang sống trước, kèm worker vừa rời) và số liệu tổng."""
    res = _res(request)
    workers = await list_workers(res.redis, Keys(res.settings.redis_prefix), include_gone=True)
    return WorkersPageOut(
        workers=workers, summary=_summary(workers), now_ms=int(time.time() * 1000)
    )


async def _set_drain(request: Request, worker_id: str, *, drain: bool) -> DrainOut:
    res = _res(request)
    keys = Keys(res.settings.redis_prefix)
    worker = await load_worker(res.redis, keys, worker_id)
    # Huỷ yêu cầu dừng vẫn được khi worker đã rời hẳn nhưng còn trong danh sách dừng.
    if worker is None and (drain or not await is_draining(res.redis, keys, worker_id)):
        raise HTTPException(404, "Không có worker này")
    await request_drain(res.redis, keys, worker_id, drain=drain)
    user = _user(request)
    log.info("worker.drain", worker_id=worker_id, drain=drain, user=user)
    await audit.record_safely(
        res.sessionmaker,
        action=audit.WORKER_DRAIN if drain else audit.WORKER_UNDRAIN,
        actor=user,
        resource_type="worker",
        resource_id=worker_id,
        metadata={"hostname": worker.hostname, "pool": worker.pool}
        if worker is not None
        else {"gone": True},
    )
    return DrainOut(worker_id=worker_id, draining=drain)


@router.post("/workers/{worker_id}/drain")
async def drain_worker(request: Request, worker_id: str) -> DrainOut:
    """Yêu cầu worker dừng nhận việc mới (làm nốt việc đang chạy)."""
    return await _set_drain(request, worker_id, drain=True)


@router.delete("/workers/{worker_id}/drain")
async def undrain_worker(request: Request, worker_id: str) -> DrainOut:
    """Huỷ yêu cầu dừng: worker nhận việc lại."""
    return await _set_drain(request, worker_id, drain=False)
