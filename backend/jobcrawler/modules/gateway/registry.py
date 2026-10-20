"""Danh sách worker lấy trang đang kết nối (Redis), dùng chung cho gateway, backend và giao diện.

Khoá Redis (tiền tố `<REDIS_PREFIX>:gw`):
- `workers`: sorted set worker_id → hạn báo sống (ms epoch, theo đồng hồ Redis `TIME`: gateway /
  backend lệch giờ không làm worker bị coi là chết sớm). Quá hạn = đã rời / đã chết.
- `worker:<id>`: JSON `WorkerStatus` (thông tin lúc đăng ký + số liệu lần báo sống gần nhất),
  TTL = hạn báo sống × 10 để trang Workers vẫn thấy worker vừa rời một lúc.
- `drain`: set worker_id được yêu cầu dừng nhận việc (làm nốt rồi rời).
- `config`: JSON cấu hình đẩy xuống worker kèm `version` (đổi ở trang Cài đặt → version mới).
- `count:<id>`: hash `leased` / `completed` / `dropped` / `lost` (HINCRBY: Lease và Complete chạy
  song song, nhiều gateway), cùng TTL với `worker:<id>`; `load_worker` / `list_workers` ghép vào
  `WorkerStatus`.
- `totals`: hash cùng các trường, cộng dồn mọi worker, không TTL (chỉ tăng).
- `inflight:<id>`: hash task_id → `<deadline_ms>:<leased_ms>` của việc worker đang giữ (Lease ghi
  cùng lúc lấy việc khỏi stream, Complete xoá; worker rời / quá hạn báo sống / không còn kể việc
  trong Heartbeat thì gateway trả `ERROR_KIND_WORKER_LOST` cho việc đó).
- `fetch:<pool>:<kind>[:hi]`: stream việc lấy trang (`:hi`: việc ưu tiên, giao trước).
- `progress:<pool>:<kind>`: số entry gateway đã lấy khỏi stream của pool / loại việc (INCRBY mỗi
  Lease); backend thấy số này tăng thì hàng việc còn chạy, chờ tiếp.
"""

from __future__ import annotations

import json
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis

DEFAULT_POOL = "default"


def _now_ms() -> int:
    return int(time.time() * 1000)


async def redis_now_ms(redis: Redis) -> int:
    """Mốc hiện tại (ms) theo đồng hồ Redis: mốc chung để xét hạn báo sống."""
    seconds, micros = await redis.time()
    return int(seconds) * 1000 + int(micros) // 1000


# Gia hạn báo sống theo đồng hồ Redis. KEYS: workers, worker:<id>, count:<id>. ARGV: worker_id,
# hạn báo sống (ms), thời gian giữ thông tin (ms), JSON thông tin (rỗng: không ghi lại).
_ALIVE = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
if ARGV[4] ~= '' then
  redis.call('SET', KEYS[2], ARGV[4], 'PX', ARGV[3])
else
  redis.call('PEXPIRE', KEYS[2], ARGV[3])
end
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[2]), ARGV[1])
redis.call('PEXPIRE', KEYS[3], ARGV[3])
return now
"""


class Keys:
    """Tên khoá Redis của gateway và hàng việc lấy trang."""

    def __init__(self, prefix: str) -> None:
        self.base = f"{prefix}:gw"
        self.workers = f"{self.base}:workers"
        self.drain = f"{self.base}:drain"
        self.config = f"{self.base}:config"
        # Tổng cộng dồn mọi worker (`leased` / `completed` / `dropped` / `lost`), không TTL.
        self.totals = f"{self.base}:totals"

    def worker(self, worker_id: str) -> str:
        return f"{self.base}:worker:{worker_id}"

    def counts(self, worker_id: str) -> str:
        """Hash số việc gateway đã giao (`leased`) / nhận kết quả (`completed`) của worker."""
        return f"{self.base}:count:{worker_id}"

    def stream(self, pool: str, kind: str, *, high: bool = False) -> str:
        """Stream việc lấy trang của một pool; `kind`: `http` | `render`; `high`: stream ưu tiên."""
        return f"{self.base}:fetch:{pool}:{kind}" + (":hi" if high else "")

    def streams(self, pool: str, kind: str) -> tuple[str, str]:
        """(stream ưu tiên, stream thường) của một pool / loại việc, theo thứ tự giao."""
        return self.stream(pool, kind, high=True), self.stream(pool, kind)

    def inflight(self, worker_id: str) -> str:
        """Hash task_id → deadline_ms của việc worker đang giữ."""
        return f"{self.base}:inflight:{worker_id}"

    def progress(self, pool: str, kind: str) -> str:
        """Bộ đếm entry đã lấy khỏi stream của pool / loại việc (cả stream ưu tiên)."""
        return f"{self.base}:progress:{pool}:{kind}"

    def reply(self, client_id: str) -> str:
        """List kết quả trả về một process backend (client_id) đang chờ."""
        return f"{self.base}:reply:{client_id}"


# `GET /api/workers` trả nguyên hai mô hình này: trường có mặc định luôn có trong JSON, spec OpenAPI
# ghi là bắt buộc (`modules/*/schemas.py`).
API_OUTPUT = ConfigDict(json_schema_serialization_defaults_required=True)


class WorkerStats(BaseModel):
    model_config = API_OUTPUT

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
    # Render tạm tắt vì lỗi Chromium (rỗng = render được); worker thử lại lúc `render_retry_at_ms`
    # (0 = tắt hẳn, vd. máy chưa cài Chromium).
    render_disabled_reason: str = ""
    render_retry_at_ms: int = 0


class WorkerStatus(BaseModel):
    model_config = API_OUTPUT

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
    # Số việc gateway đã giao / nhận kết quả (đếm phía gateway); `dropped`: kết quả bị bỏ vì
    # worker không còn giữ việc; `lost`: việc trả về backend khi worker rời / quá hạn báo sống.
    leased: int = 0
    completed: int = 0
    dropped: int = 0
    lost: int = 0
    stats: WorkerStats = Field(default_factory=WorkerStats)
    alive: bool = False
    draining: bool = False


async def _alive(redis: Redis, keys: Keys, worker_id: str, ttl: float, data: str) -> None:
    await redis.eval(  # pyright: ignore[reportGeneralTypeIssues]
        _ALIVE,
        3,
        keys.workers,
        keys.worker(worker_id),
        keys.counts(worker_id),
        worker_id,
        int(ttl * 1000),
        int(ttl * 10_000),
        data,
    )


async def save_worker(redis: Redis, keys: Keys, status: WorkerStatus, *, ttl: float) -> None:
    """Ghi thông tin worker và gia hạn báo sống `ttl` giây."""
    status.last_seen_ms = _now_ms()
    data = status.model_dump(mode="json", exclude={"alive", "draining"})
    await _alive(redis, keys, status.worker_id, ttl, json.dumps(data))


async def touch_worker(redis: Redis, keys: Keys, worker_id: str, *, ttl: float) -> None:
    """Gia hạn báo sống `ttl` giây, không ghi lại JSON (mỗi lần Lease)."""
    await _alive(redis, keys, worker_id, ttl, "")


COUNT_FIELDS = ("leased", "completed", "dropped", "lost")


async def add_counts(
    redis: Redis,
    keys: Keys,
    worker_id: str,
    *,
    ttl: float,
    leased: int = 0,
    completed: int = 0,
    dropped: int = 0,
    lost: int = 0,
) -> None:
    """Cộng số việc đã giao / đã nhận kết quả / kết quả bị bỏ / việc mất của worker (và vào tổng
    cộng dồn `keys.totals`)."""
    key = keys.counts(worker_id)
    values = {"leased": leased, "completed": completed, "dropped": dropped, "lost": lost}
    async with redis.pipeline(transaction=False) as pipe:
        for name, value in values.items():
            if value:
                pipe.hincrby(key, name, value)
                pipe.hincrby(keys.totals, name, value)
        pipe.pexpire(key, int(ttl * 10_000))
        await pipe.execute()


async def load_totals(redis: Redis, keys: Keys) -> dict[str, int]:
    """Tổng cộng dồn mọi worker từ khi Redis có khoá `totals` (chỉ tăng)."""
    raw = await redis.hmget(keys.totals, list(COUNT_FIELDS))  # pyright: ignore[reportGeneralTypeIssues]
    return {name: int(v or 0) for name, v in zip(COUNT_FIELDS, raw, strict=True)}


def _with_counts(status: WorkerStatus, counts: list[Any]) -> WorkerStatus:
    for name, value in zip(COUNT_FIELDS, counts, strict=True):
        if value is not None:
            setattr(status, name, int(value))
    return status


async def load_worker(redis: Redis, keys: Keys, worker_id: str) -> WorkerStatus | None:
    async with redis.pipeline(transaction=False) as pipe:
        pipe.get(keys.worker(worker_id))
        pipe.hmget(keys.counts(worker_id), list(COUNT_FIELDS))
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
    now = await redis_now_ms(redis)
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
            pipe.hmget(keys.counts(worker_id), list(COUNT_FIELDS))
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
    alive = await redis.zrangebyscore(keys.workers, await redis_now_ms(redis), "+inf")
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
    alive = await redis.zrangebyscore(keys.workers, await redis_now_ms(redis), "+inf")
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
    """Worker quá hạn báo sống nhưng chưa bị prune (`dispatch.release_expired_worker`)."""
    now = await redis_now_ms(redis)
    return [_text(w) for w in await redis.zrangebyscore(keys.workers, "-inf", now)]


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
