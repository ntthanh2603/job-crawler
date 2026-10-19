"""API trang Cài đặt và trang Workers (`/api/settings`, `/api/workers`).

Cài đặt: giá trị gốc là `app.state.settings` (biến môi trường / .env lúc API khởi động), đè bởi
bảng `app_settings` (`services.app_settings`). Workers: đọc danh sách worker lấy trang trong Redis
(`gateway.registry`), yêu cầu worker dừng nhận việc / nhận việc lại.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

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
from jobcrawler.services import app_settings

router = APIRouter(prefix="/api", tags=["settings"])

log = get_logger(__name__)


def _res(request: Request) -> Resources:
    return request.app.state.res


def _base(request: Request) -> Settings:
    """Settings gốc của process API (chưa đè cài đặt từ giao diện)."""
    return request.app.state.settings


def _user(request: Request) -> str | None:
    return getattr(request.state, "user", None)


# --- cài đặt -------------------------------------------------------------------------------------


class SettingsUpdate(BaseModel):
    # Tên cài đặt → giá trị mới; `null` = về giá trị biến môi trường / mặc định.
    values: dict[str, Any]


async def _settings_payload(request: Request) -> dict[str, Any]:
    res, base = _res(request), _base(request)
    overrides = await app_settings.load_overrides(res.sessionmaker)
    effective = app_settings.apply_overrides(base, overrides)
    return {
        "version": await app_settings.settings_version(res.redis, base.redis_prefix),
        "groups": app_settings.describe(base, overrides, effective),
    }


async def _save(request: Request, values: dict[str, Any]) -> dict[str, Any]:
    res, base = _res(request), _base(request)
    before = await app_settings.load_effective(res.sessionmaker, base)
    try:
        after = await app_settings.save(
            res.sessionmaker, res.redis, base.redis_prefix, base, values, user=_user(request)
        )
    except app_settings.SettingsError as exc:
        raise HTTPException(422, detail={"errors": exc.errors}) from exc
    payload = await _settings_payload(request)
    payload["restart_required"] = app_settings.restart_required(before, after, values)
    return payload


@router.get("/settings")
async def get_settings(request: Request) -> dict[str, Any]:
    """Mọi cài đặt sửa được, theo nhóm: giá trị hiệu lực, mặc định, từ biến môi trường, nguồn."""
    return await _settings_payload(request)


@router.put("/settings")
async def put_settings(request: Request, body: SettingsUpdate) -> dict[str, Any]:
    """Lưu nhiều cài đặt cùng lúc (kiểm tra tất cả trước, lỗi thì không lưu gì: 422 kèm
    `detail.errors` = tên cài đặt → thông báo). `restart_required`: các cài đặt vừa đổi chỉ có
    hiệu lực sau khi khởi động lại service."""
    return await _save(request, body.values)


@router.delete("/settings/{key}")
async def reset_setting(request: Request, key: str) -> dict[str, Any]:
    """Bỏ giá trị đặt ở giao diện, về giá trị biến môi trường / mặc định."""
    if key not in app_settings.SPEC_BY_KEY:
        raise HTTPException(404, "Không có cài đặt này hoặc cài đặt chỉ đặt bằng biến môi trường")
    return await _save(request, {key: None})


# --- worker lấy trang ----------------------------------------------------------------------------


def _summary(workers: list[WorkerStatus]) -> dict[str, Any]:
    alive = [w for w in workers if w.alive]
    pools: dict[str, dict[str, int]] = {}
    for w in alive:
        pool = pools.setdefault(
            w.pool,
            {
                "workers": 0,
                "http_slots": 0,
                "render_slots": 0,
                "http_inflight": 0,
                "render_inflight": 0,
            },
        )
        pool["workers"] += 1
        pool["http_slots"] += w.http_slots
        pool["render_slots"] += w.render_slots
        pool["http_inflight"] += w.stats.http_inflight
        pool["render_inflight"] += w.stats.render_inflight
    return {
        "alive": len(alive),
        "gone": len(workers) - len(alive),
        "draining": sum(1 for w in alive if w.draining),
        "pools": pools,
        "http_slots": sum(p["http_slots"] for p in pools.values()),
        "render_slots": sum(p["render_slots"] for p in pools.values()),
        "http_inflight": sum(p["http_inflight"] for p in pools.values()),
        "render_inflight": sum(p["render_inflight"] for p in pools.values()),
        "done": sum(w.stats.http_done + w.stats.render_done for w in workers),
        "failed": sum(w.stats.http_failed + w.stats.render_failed for w in workers),
    }


@router.get("/workers")
async def get_workers(request: Request) -> dict[str, Any]:
    """Worker lấy trang đã biết (đang sống trước, kèm worker vừa rời) và số liệu tổng."""
    res = _res(request)
    workers = await list_workers(res.redis, Keys(res.settings.redis_prefix), include_gone=True)
    return {
        "workers": [w.model_dump(mode="json") for w in workers],
        "summary": _summary(workers),
        "now_ms": int(time.time() * 1000),
    }


async def _set_drain(request: Request, worker_id: str, *, drain: bool) -> dict[str, Any]:
    res = _res(request)
    keys = Keys(res.settings.redis_prefix)
    known = await load_worker(res.redis, keys, worker_id) is not None
    # Huỷ yêu cầu dừng vẫn được khi worker đã rời hẳn nhưng còn trong danh sách dừng.
    if not known and (drain or not await is_draining(res.redis, keys, worker_id)):
        raise HTTPException(404, "Không có worker này")
    await request_drain(res.redis, keys, worker_id, drain=drain)
    log.info("worker.drain", worker_id=worker_id, drain=drain, user=_user(request))
    return {"worker_id": worker_id, "draining": drain}


@router.post("/workers/{worker_id}/drain")
async def drain_worker(request: Request, worker_id: str) -> dict[str, Any]:
    """Yêu cầu worker dừng nhận việc mới (làm nốt việc đang chạy)."""
    return await _set_drain(request, worker_id, drain=True)


@router.delete("/workers/{worker_id}/drain")
async def undrain_worker(request: Request, worker_id: str) -> dict[str, Any]:
    """Huỷ yêu cầu dừng: worker nhận việc lại."""
    return await _set_drain(request, worker_id, drain=False)
