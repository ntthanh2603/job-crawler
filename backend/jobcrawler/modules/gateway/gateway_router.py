"""API trang Workers (`/api/workers…`): đọc danh sách worker lấy trang trong Redis
(`modules.gateway.registry_service`), khoá tham gia, yêu cầu worker dừng nhận việc / nhận việc
lại."""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request

from jobcrawler.common.api import actor_of, res_of, settings_of
from jobcrawler.common.config import Settings
from jobcrawler.common.logging import get_logger
from jobcrawler.modules.audit import audit_service
from jobcrawler.modules.gateway.gateway_schema import (
    DrainOut,
    JoinKeyOut,
    JoinKeyStatusOut,
    WorkerPoolOut,
    WorkersPageOut,
    WorkersSummaryOut,
)
from jobcrawler.modules.gateway.registry_service import (
    Keys,
    WorkerStatus,
    is_draining,
    list_workers,
    load_worker,
    request_drain,
)
from jobcrawler.modules.gateway.worker_auth_service import JOIN_TTL_SECONDS, WorkerAuth
from jobcrawler.services.resources import Resources

router = APIRouter(prefix="/api", tags=["workers"])

log = get_logger(__name__)


def _worker_auth_of(res: Resources, base: Settings) -> WorkerAuth:
    """Xác thực worker dùng chung Redis với gateway (khoá tham gia nằm trên Redis chính)."""
    token = base.gateway_token.get_secret_value() if base.gateway_token else None
    return WorkerAuth(res.redis, Keys(base.redis_prefix).base, static_token=token)


def _worker_auth(request: Request) -> WorkerAuth:
    return _worker_auth_of(res_of(request), settings_of(request))


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
    """Như `GET /api/workers` (cũng là chủ đề `workers` của `modules/live/live_service.py`). `base`:
    Settings gốc của process API (GATEWAY_TOKEN chỉ đặt bằng biến môi trường)."""
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
    return await load_workers_page(res_of(request), settings_of(request))


@router.post("/workers/join-keys")
async def create_join_key(request: Request) -> JoinKeyOut:
    """Tạo khoá tham gia dùng một lần (hết hạn sau 15 phút) kèm cấu hình cho lệnh `docker run`.
    Chỉ khi gateway bật xác thực; khoá chỉ trả một lần, máy chủ chỉ giữ dạng băm."""
    auth = _worker_auth(request)
    if not auth.enabled:
        raise HTTPException(
            409, "Gateway đang không hỏi khoá: worker nối vào không cần khoá tham gia"
        )
    user = actor_of(request)
    join = await auth.create_join_key(created_by=user)
    await audit_service.record_safely(
        res_of(request).sessionmaker,
        action=audit_service.WORKER_JOIN_KEY,
        actor=user,
        resource_type="join_key",
        resource_id=join.id,
        metadata={"ttl_seconds": JOIN_TTL_SECONDS},
    )
    settings = res_of(request).settings
    return JoinKeyOut(
        id=join.id,
        key=join.key,
        expires_at_ms=join.expires_at_ms,
        ttl_seconds=JOIN_TTL_SECONDS,
        gateway_url=settings.gateway_public_url,
        worker_image=settings.gateway_worker_image,
    )


@router.get("/workers/join-keys/{join_id}")
async def get_join_key(request: Request, join_id: str) -> JoinKeyStatusOut:
    """Khoá tham gia còn chờ, đã được worker dùng, hay hết hạn / đã huỷ."""
    status, worker_id = await _worker_auth(request).join_key_status(join_id)
    return JoinKeyStatusOut(id=join_id, status=status, worker_id=worker_id)


@router.delete("/workers/join-keys/{join_id}")
async def revoke_join_key(request: Request, join_id: str) -> JoinKeyStatusOut:
    """Huỷ khoá tham gia chưa dùng (đóng hộp thoại, tạo khoá mới)."""
    auth = _worker_auth(request)
    if not await auth.revoke_join_key(join_id):
        # Không có gì để huỷ: hết hạn, hoặc worker vừa dùng khoá (trả `used`, không ghi nhật ký).
        status, worker_id = await auth.join_key_status(join_id)
        return JoinKeyStatusOut(id=join_id, status=status, worker_id=worker_id)
    await audit_service.record_safely(
        res_of(request).sessionmaker,
        action=audit_service.WORKER_JOIN_KEY_REVOKE,
        actor=actor_of(request),
        resource_type="join_key",
        resource_id=join_id,
    )
    return JoinKeyStatusOut(id=join_id, status="expired", worker_id=None)


async def _set_drain(request: Request, worker_id: str, *, drain: bool) -> DrainOut:
    res = res_of(request)
    keys = Keys(res.settings.redis_prefix)
    worker = await load_worker(res.redis, keys, worker_id)
    # Huỷ yêu cầu dừng vẫn được khi worker đã rời hẳn nhưng còn trong danh sách dừng.
    if worker is None and (drain or not await is_draining(res.redis, keys, worker_id)):
        raise HTTPException(404, "Không có worker này")
    await request_drain(res.redis, keys, worker_id, drain=drain)
    user = actor_of(request)
    log.info("worker.drain", worker_id=worker_id, drain=drain, user=user)
    await audit_service.record_safely(
        res.sessionmaker,
        action=audit_service.WORKER_DRAIN if drain else audit_service.WORKER_UNDRAIN,
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
