"""API trang Cài đặt và trang Workers (`/api/settings`, `/api/proxies`, `/api/workers`).

Cài đặt: giá trị gốc là `app.state.settings` (biến môi trường / .env lúc API khởi động), đè bởi
bảng `app_settings` (`services.app_settings`). Proxy chung: trạng thái xoay vòng và kiểm tra qua
worker (`services.proxies`). Workers: đọc danh sách worker lấy trang trong Redis
(`gateway.registry`), yêu cầu worker dừng nhận việc / nhận việc lại.
"""

from __future__ import annotations

import dataclasses
import time
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, WithJsonSchema

from jobcrawler.config import Settings
from jobcrawler.gateway.registry import (
    Keys,
    WorkerStatus,
    is_draining,
    list_workers,
    load_worker,
    request_drain,
)
from jobcrawler.gateway.worker_auth import JOIN_TTL_SECONDS, WorkerAuth
from jobcrawler.logging import get_logger
from jobcrawler.resources import Resources
from jobcrawler.services import app_settings, audit
from jobcrawler.services import proxies as proxy_service
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
    live: bool  # False: chỉ có hiệu lực sau khi khởi động lại process (`jobcrawler serve` tự làm)
    value: SettingValue  # bí mật luôn null (xem `is_set`)
    default: SettingValue
    env_value: SettingValue
    source: app_settings.Source  # giá trị đang dùng lấy từ đâu
    secret: bool
    is_set: bool | None  # chỉ với bí mật
    env_is_set: bool | None
    # Cài đặt mà 0 = tự tính theo máy: giá trị tự động đó (trên máy chạy backend); null = không có.
    auto_value: int | None


class SettingGroupOut(ApiOut):
    group: str
    title: str
    description: str
    sections: list[SettingSectionOut]
    items: list[SettingItemOut]


class SettingsPageOut(ApiOut):
    version: int  # tăng mỗi lần lưu; 0 khi chưa lưu lần nào
    groups: list[SettingGroupOut]
    # Cài đặt đang lấy từ biến môi trường / .env (khác mặc định), chưa lưu trong DB.
    env_keys: list[str]
    # Backend chạy dưới `jobcrawler serve`: cài đặt `live=False` được tự áp dụng (khởi động lại
    # process con); False = phải khởi động lại backend.
    auto_restart: bool
    # Đã lưu cài đặt `live=False` mà backend chưa khởi động lại xong (chỉ khi auto_restart).
    restart_pending: bool


class SettingsSavedOut(SettingsPageOut):
    restart_required: list[str]  # cài đặt vừa đổi chỉ có hiệu lực sau khi khởi động lại


class SettingsImportedOut(SettingsSavedOut):
    imported: list[str]  # đã chép từ biến môi trường vào DB
    skipped: dict[str, str]  # không hợp lệ ở giao diện: key → lý do (vẫn lấy từ biến môi trường)


async def _settings_page(request: Request) -> SettingsPageOut:
    res, base = _res(request), _base(request)
    prefix = base.redis_prefix
    overrides = await app_settings.load_overrides(res.sessionmaker)
    effective = app_settings.apply_overrides(base, overrides)
    restart = await app_settings.restart_state(res.redis, prefix)
    return SettingsPageOut.model_validate(
        {
            "version": await app_settings.settings_version(res.redis, prefix),
            "groups": app_settings.describe(base, overrides, effective),
            "env_keys": sorted(app_settings.env_overrides(base, overrides)),
            "auto_restart": restart.auto,
            "restart_pending": restart.pending,
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
        **page.model_dump(),
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


@router.post("/settings/import-env")
async def import_env_settings(request: Request) -> SettingsImportedOut:
    """Chép các cài đặt đang lấy từ biến môi trường / .env của backend (`env_keys`) vào DB để quản
    lý ở trang này; giá trị đang dùng không đổi. Giá trị không hợp lệ ở giao diện thì bỏ qua."""
    res, base = _res(request), _base(request)
    result = await app_settings.import_env(
        res.sessionmaker, res.redis, base.redis_prefix, base, user=_user(request)
    )
    page = await _settings_page(request)
    return SettingsImportedOut(
        **page.model_dump(), restart_required=[], imported=result.keys, skipped=result.skipped
    )


@router.delete("/settings/{key}")
async def reset_setting(request: Request, key: str) -> SettingsSavedOut:
    """Bỏ giá trị đặt ở giao diện, về giá trị biến môi trường / mặc định."""
    if key not in app_settings.SPEC_BY_KEY:
        raise HTTPException(404, "Không có cài đặt này hoặc cài đặt chỉ đặt bằng biến môi trường")
    return await _save(request, {key: None})


# --- proxy chung ---------------------------------------------------------------------------------


class ProxyStatusOut(ApiOut):
    """Một proxy chung và trạng thái xoay vòng của nó (`services.proxies.ProxyStatus`)."""

    id: str
    line: str  # đã che mật khẩu, khớp một dòng của cài đặt `proxies`
    source: str  # "settings" | "file"
    scheme: str
    host: str
    port: int
    username: str | None
    has_password: bool
    score: int
    fails: int
    dead: bool
    dead_reason: str | None
    dead_seconds: int | None
    cooling_domains: int
    sticky_domains: int


class ProxiesOut(ApiOut):
    # Chế độ đang dùng: chỉ "central" lấy proxy từ danh sách này.
    mode: Literal["worker", "central", "off"]
    check_url: str
    items: list[ProxyStatusOut]


class ProxyCheckIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=proxy_service.CHECK_BATCH)


class ProxyCheckOut(ApiOut):
    """Kết quả gọi thử qua một proxy (`services.health.ProxyCheck`)."""

    proxy_id: str
    proxy: str  # đã che mật khẩu
    ok: bool
    status: int | None
    latency_ms: int | None
    exit_ip: str | None
    location: str | None
    error: str | None


class ProxyChecksOut(ApiOut):
    results: list[ProxyCheckOut]


@router.get("/proxies")
async def get_proxies(request: Request) -> ProxiesOut:
    """Danh sách proxy chung (cài đặt `proxies` rồi PROXIES_FILE) kèm trạng thái xoay vòng."""
    res, base = _res(request), _base(request)
    settings = await app_settings.load_effective(res.sessionmaker, base)
    items = await proxy_service.statuses(res.redis, settings)
    return ProxiesOut.model_validate(
        {
            "mode": settings.proxy_mode,
            "check_url": settings.proxy_check_url,
            "items": [dataclasses.asdict(i) for i in items],
        }
    )


@router.post("/proxies/check")
async def check_proxies(request: Request, body: ProxyCheckIn) -> ProxyChecksOut:
    """Gọi trang kiểm tra qua từng proxy trong `ids` (tối đa 20 mỗi lần) từ worker. Proxy sống
    được đưa lại vào vòng xoay, proxy lỗi bị loại. Không có worker thì báo lỗi, không loại proxy."""
    res, base = _res(request), _base(request)
    settings = await app_settings.load_effective(res.sessionmaker, base)
    results = await proxy_service.check(res, settings, body.ids)
    log.info(
        "proxies.checked",
        user=_user(request),
        total=len(results),
        ok=sum(r.ok for r in results),
    )
    return ProxyChecksOut.model_validate({"results": [dataclasses.asdict(r) for r in results]})


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
    # Gateway hỏi khoá khi worker nối vào (GATEWAY_TOKEN); False = worker nào cũng vào được.
    auth_enabled: bool


class JoinKeyOut(ApiOut):
    """Khoá tham gia dùng một lần cho một máy worker mới (chỉ trả `key` lúc tạo)."""

    id: str
    key: str
    expires_at_ms: int
    ttl_seconds: int


class JoinKeyStatusOut(ApiOut):
    id: str
    status: Literal["pending", "used", "expired"]
    worker_id: str | None  # worker đã dùng khoá (khi `used`)


class DrainOut(ApiOut):
    worker_id: str
    draining: bool


def _worker_auth_of(res: Resources, base: Settings) -> WorkerAuth:
    """Xác thực worker dùng chung Redis với gateway (khoá tham gia nằm trên Redis chính)."""
    token = base.gateway_token.get_secret_value() if base.gateway_token else None
    return WorkerAuth(res.redis, Keys(base.redis_prefix).base, static_token=token)


def _worker_auth(request: Request) -> WorkerAuth:
    return _worker_auth_of(_res(request), _base(request))


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


async def load_workers_page(res: Resources, base: Settings) -> WorkersPageOut:
    """Như `GET /api/workers` (cũng là chủ đề `workers` của `web/live.py`). `base`: Settings gốc
    của process API (GATEWAY_TOKEN chỉ đặt bằng biến môi trường)."""
    workers = await list_workers(res.redis, Keys(res.settings.redis_prefix), include_gone=True)
    return WorkersPageOut(
        workers=workers,
        summary=_summary(workers),
        now_ms=int(time.time() * 1000),
        auth_enabled=_worker_auth_of(res, base).enabled,
    )


@router.get("/workers")
async def get_workers(request: Request) -> WorkersPageOut:
    """Worker lấy trang đã biết (đang sống trước, kèm worker vừa rời) và số liệu tổng."""
    return await load_workers_page(_res(request), _base(request))


@router.post("/workers/join-keys")
async def create_join_key(request: Request) -> JoinKeyOut:
    """Tạo khoá tham gia dùng một lần (hết hạn sau 5 phút) cho máy worker mới. Chỉ khi gateway
    bật xác thực; khoá chỉ trả một lần, trên máy chủ chỉ giữ dạng băm."""
    auth = _worker_auth(request)
    if not auth.enabled:
        raise HTTPException(
            409, "Gateway đang không hỏi khoá: worker nối vào không cần khoá tham gia"
        )
    user = _user(request)
    join = await auth.create_join_key(created_by=user)
    await audit.record_safely(
        _res(request).sessionmaker,
        action=audit.WORKER_JOIN_KEY,
        actor=user,
        resource_type="join_key",
        resource_id=join.id,
        metadata={"ttl_seconds": JOIN_TTL_SECONDS},
    )
    return JoinKeyOut(
        id=join.id, key=join.key, expires_at_ms=join.expires_at_ms, ttl_seconds=JOIN_TTL_SECONDS
    )


@router.get("/workers/join-keys/{join_id}")
async def get_join_key(request: Request, join_id: str) -> JoinKeyStatusOut:
    """Khoá tham gia còn chờ, đã được worker dùng, hay hết hạn / đã huỷ."""
    status, worker_id = await _worker_auth(request).join_key_status(join_id)
    return JoinKeyStatusOut(id=join_id, status=status, worker_id=worker_id)


@router.delete("/workers/join-keys/{join_id}")
async def revoke_join_key(request: Request, join_id: str) -> JoinKeyStatusOut:
    """Huỷ khoá tham gia chưa dùng (đóng hộp thoại, tạo khoá mới)."""
    revoked = await _worker_auth(request).revoke_join_key(join_id)
    if revoked:
        await audit.record_safely(
            _res(request).sessionmaker,
            action=audit.WORKER_JOIN_KEY_REVOKE,
            actor=_user(request),
            resource_type="join_key",
            resource_id=join_id,
        )
    return JoinKeyStatusOut(id=join_id, status="expired", worker_id=None)


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
