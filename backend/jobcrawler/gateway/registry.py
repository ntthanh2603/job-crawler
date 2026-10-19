"""Danh sách worker lấy trang đang kết nối (Redis), dùng chung cho gateway, backend và giao diện.

Khoá Redis (tiền tố `<REDIS_PREFIX>:gw`):
- `workers`: sorted set worker_id → hạn báo sống (ms epoch). Quá hạn = đã rời / đã chết.
- `worker:<id>`: JSON `WorkerStatus` (thông tin lúc đăng ký + số liệu lần báo sống gần nhất),
  TTL = hạn báo sống × 10 để trang Workers vẫn thấy worker vừa rời một lúc.
- `drain`: set worker_id được yêu cầu dừng nhận việc (làm nốt rồi rời).
- `config`: JSON cấu hình đẩy xuống worker kèm `version` (đổi ở trang Cài đặt → version mới).
- `count:<id>`: hash `leased` / `completed` (HINCRBY: Lease và Complete chạy song song, nhiều
  gateway), cùng TTL với `worker:<id>`; `load_worker` / `list_workers` ghép vào `WorkerStatus`.
"""

from __future__ import annotations

import json
import time
from typing import Any

from pydantic import BaseModel, Field
from redis.asyncio import Redis

DEFAULT_POOL = "default"


def _now_ms() -> int:
    return int(time.time() * 1000)


class Keys:
    """Tên khoá Redis của gateway và hàng việc lấy trang."""

    def __init__(self, prefix: str) -> None:
        self.base = f"{prefix}:gw"
        self.workers = f"{self.base}:workers"
        self.drain = f"{self.base}:drain"
        self.config = f"{self.base}:config"

    def worker(self, worker_id: str) -> str:
        return f"{self.base}:worker:{worker_id}"

    def counts(self, worker_id: str) -> str:
        """Hash số việc gateway đã giao (`leased`) / nhận kết quả (`completed`) của worker."""
        return f"{self.base}:count:{worker_id}"

    def stream(self, pool: str, kind: str) -> str:
        """Stream việc lấy trang của một pool; `kind`: `http` | `render`."""
        return f"{self.base}:fetch:{pool}:{kind}"

    def reply(self, client_id: str) -> str:
        """List kết quả trả về một process backend (client_id) đang chờ."""
        return f"{self.base}:reply:{client_id}"


class WorkerStats(BaseModel):
    http_done: int = 0
    http_failed: int = 0
    render_done: int = 0
    render_failed: int = 0
    http_inflight: int = 0
    render_inflight: int = 0
    bytes_in: int = 0
    cpu_percent: float = 0.0
    rss_bytes: int = 0
    proxies_alive: int = 0


class WorkerStatus(BaseModel):
    worker_id: str
    hostname: str = ""
    version: str = ""
    pool: str = DEFAULT_POOL
    http_slots: int = 0
    render_slots: int = 0
    local_proxies: int = 0
    labels: dict[str, str] = Field(default_factory=dict)
    started_at_ms: int = 0
    registered_at_ms: int = 0
    last_seen_ms: int = 0
    # Số việc gateway đã giao / nhận kết quả (đếm phía gateway).
    leased: int = 0
    completed: int = 0
    stats: WorkerStats = Field(default_factory=WorkerStats)
    alive: bool = False
    draining: bool = False


async def save_worker(redis: Redis, keys: Keys, status: WorkerStatus, *, ttl: float) -> None:
    """Ghi thông tin worker và gia hạn báo sống `ttl` giây."""
    now = _now_ms()
    status.last_seen_ms = now
    data = status.model_dump(mode="json", exclude={"alive", "draining"})
    async with redis.pipeline(transaction=False) as pipe:
        pipe.set(keys.worker(status.worker_id), json.dumps(data), px=int(ttl * 10_000))
        pipe.zadd(keys.workers, {status.worker_id: now + int(ttl * 1000)})
        pipe.pexpire(keys.counts(status.worker_id), int(ttl * 10_000))
        await pipe.execute()


async def touch_worker(redis: Redis, keys: Keys, worker_id: str, *, ttl: float) -> None:
    """Gia hạn báo sống `ttl` giây, không ghi lại JSON (mỗi lần Lease)."""
    async with redis.pipeline(transaction=False) as pipe:
        pipe.zadd(keys.workers, {worker_id: _now_ms() + int(ttl * 1000)})
        pipe.pexpire(keys.worker(worker_id), int(ttl * 10_000))
        pipe.pexpire(keys.counts(worker_id), int(ttl * 10_000))
        await pipe.execute()


async def add_counts(
    redis: Redis, keys: Keys, worker_id: str, *, ttl: float, leased: int = 0, completed: int = 0
) -> None:
    """Cộng số việc đã giao / đã nhận kết quả của worker."""
    key = keys.counts(worker_id)
    async with redis.pipeline(transaction=False) as pipe:
        if leased:
            pipe.hincrby(key, "leased", leased)
        if completed:
            pipe.hincrby(key, "completed", completed)
        pipe.pexpire(key, int(ttl * 10_000))
        await pipe.execute()


def _with_counts(status: WorkerStatus, counts: list[Any]) -> WorkerStatus:
    leased, completed = counts
    if leased is not None:
        status.leased = int(leased)
    if completed is not None:
        status.completed = int(completed)
    return status


async def load_worker(redis: Redis, keys: Keys, worker_id: str) -> WorkerStatus | None:
    async with redis.pipeline(transaction=False) as pipe:
        pipe.get(keys.worker(worker_id))
        pipe.hmget(keys.counts(worker_id), ["leased", "completed"])
        raw, counts = await pipe.execute()
    if raw is None:
        return None
    return _with_counts(WorkerStatus.model_validate_json(raw), counts)


async def remove_worker(redis: Redis, keys: Keys, worker_id: str) -> None:
    async with redis.pipeline(transaction=False) as pipe:
        pipe.zrem(keys.workers, worker_id)
        pipe.srem(keys.drain, worker_id)
        await pipe.execute()


async def list_workers(
    redis: Redis, keys: Keys, *, include_gone: bool = True
) -> list[WorkerStatus]:
    """Worker đã biết (sống trước, theo pool rồi id). `include_gone`: kèm worker vừa rời (còn
    trong TTL của `worker:<id>`)."""
    now = _now_ms()
    members: list[tuple[Any, float]] = await redis.zrange(keys.workers, 0, -1, withscores=True)
    expiry = {_text(m): int(score) for m, score in members}
    draining = {_text(m) for m in await redis.smembers(keys.drain)}  # pyright: ignore[reportGeneralTypeIssues]
    ids = list(expiry)
    if include_gone:
        async for key in redis.scan_iter(match=keys.worker("*"), count=500):
            worker_id = _text(key).removeprefix(keys.worker(""))
            if worker_id not in expiry:
                ids.append(worker_id)
    if not ids:
        return []
    raws = await redis.mget([keys.worker(w) for w in ids])
    async with redis.pipeline(transaction=False) as pipe:
        for worker_id in ids:
            pipe.hmget(keys.counts(worker_id), ["leased", "completed"])
        counts = await pipe.execute()
    out: list[WorkerStatus] = []
    for worker_id, raw, count in zip(ids, raws, counts, strict=True):
        if raw is None:
            continue
        status = _with_counts(WorkerStatus.model_validate_json(raw), count)
        status.alive = expiry.get(worker_id, 0) > now
        status.draining = worker_id in draining
        out.append(status)
    out.sort(key=lambda w: (not w.alive, w.pool, w.worker_id))
    return out


async def live_pools(redis: Redis, keys: Keys) -> dict[str, int]:
    """Số worker còn sống theo pool."""
    alive = await redis.zrangebyscore(keys.workers, _now_ms(), "+inf")
    if not alive:
        return {}
    raws = await redis.mget([keys.worker(_text(w)) for w in alive])
    pools: dict[str, int] = {}
    for raw in raws:
        if raw is not None:
            pool = json.loads(raw).get("pool") or DEFAULT_POOL
            pools[pool] = pools.get(pool, 0) + 1
    return pools


async def live_capacity(redis: Redis, keys: Keys) -> dict[str, dict[str, int]]:
    """Số worker còn sống theo pool và loại việc: `http` (mọi worker), `render` (worker đăng ký
    `render_slots` > 0)."""
    alive = await redis.zrangebyscore(keys.workers, _now_ms(), "+inf")
    if not alive:
        return {}
    raws = await redis.mget([keys.worker(_text(w)) for w in alive])
    out: dict[str, dict[str, int]] = {}
    for raw in raws:
        if raw is None:
            continue
        data = json.loads(raw)
        kinds = out.setdefault(data.get("pool") or DEFAULT_POOL, {"http": 0, "render": 0})
        kinds["http"] += 1
        if (data.get("render_slots") or 0) > 0:
            kinds["render"] += 1
    return out


async def expired_workers(redis: Redis, keys: Keys) -> list[str]:
    """Worker quá hạn báo sống nhưng chưa bị `prune`."""
    return [_text(w) for w in await redis.zrangebyscore(keys.workers, "-inf", _now_ms())]


async def prune(redis: Redis, keys: Keys) -> int:
    """Bỏ worker quá hạn báo sống khỏi danh sách sống. Trả số worker bị bỏ."""
    return int(await redis.zremrangebyscore(keys.workers, "-inf", _now_ms()))


async def request_drain(redis: Redis, keys: Keys, worker_id: str, *, drain: bool = True) -> None:
    """Yêu cầu worker dừng nhận việc (làm nốt việc đang chạy rồi rời); `drain=False` để huỷ."""
    if drain:
        await redis.sadd(keys.drain, worker_id)  # pyright: ignore[reportGeneralTypeIssues]
    else:
        await redis.srem(keys.drain, worker_id)  # pyright: ignore[reportGeneralTypeIssues]


async def is_draining(redis: Redis, keys: Keys, worker_id: str) -> bool:
    return bool(await redis.sismember(keys.drain, worker_id))  # pyright: ignore[reportGeneralTypeIssues]


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)
