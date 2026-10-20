"""Hàng đợi job trên Redis Streams: consumer group, job có khoá duy nhất, hẹn giờ, cron.

Khoá Redis (`sq:<queue>:…`):
- `stream`: mỗi entry `{key: <khoá job>}`; consumer group `workers`, mỗi process worker là một
  consumer. Worker chỉ đọc (XREADGROUP) khi còn slot rảnh nên việc dồn lại trong stream, worker nào
  rảnh thì nhận.
- `job:<khoá>`: hash trạng thái job (`scheduled` → `queued` → `active` → `complete`/`failed`). Khoá
  job là duy nhất: job cùng khoá đang hẹn/chờ/chạy thì không xếp thêm. Job xong giữ JOB_TTL giây.
- `scheduled`, `queued`, `active`: sorted set khoá job theo trạng thái, để đếm và liệt kê trên trang
  Hàng đợi. Job hẹn giờ chỉ vào stream khi tới giờ (worker chuyển mỗi giây).
- `workers` + `worker:<id>`: worker đang sống (báo mỗi `reclaim_seconds / 6`) kèm số đếm.

Worker chết (OOM, kill): entry còn trong PEL của consumer đó. Worker sống làm mới idle của entry
mình đang chạy mỗi lần báo sống (XCLAIM JUSTID); entry idle quá `reclaim_seconds` bị XAUTOCLAIM thu
hồi: chạy lại nếu còn lượt (`retries` = số lần chạy tối đa), hết lượt thì `failed`. Mọi
chuyển trạng thái chạy trong Lua và so `entry` (id entry đổi mỗi lần xếp lại: đóng vai fencing
token của lượt chạy), nên lượt cũ (đã thu hồi) không ghi đè trạng thái job mới.

Lượt bị thu hồi khi worker vẫn sống (event loop đứng quá `reclaim_seconds`, mất Redis) tự dừng: mỗi
lần báo sống, Lua chỉ làm mới entry mà hash job còn ghi đúng `entry` + worker + `active`; entry
không còn là của mình thì worker huỷ lượt đó (`queue.job_revoked`) và không ghi gì về job nữa.

Mốc dùng để xét sống / thu hồi (hạn báo sống, `started`) theo đồng hồ của Redis (`TIME`), không
theo đồng hồ từng máy: máy lệch giờ không làm job bị thu hồi sớm.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import socket
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, tzinfo
from typing import Any

from croniter import croniter
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from jobcrawler.common.logging import get_logger

log = get_logger(__name__)

GROUP = "workers"
JOB_TTL = 600  # giây giữ job đã xong
TERMINAL = ("complete", "failed", "aborted")
BLOCK_MS = 1000  # XREADGROUP chờ tối đa chừng này rồi xem lại cờ dừng
READ_RETRY_SECONDS = 1.0  # đọc stream lỗi (Redis mất) thì chờ chừng này rồi thử lại
SCHEDULE_BATCH = 500
# Consumer không còn entry đang giữ và worker đã thôi báo sống chừng này ms thì xoá khỏi group.
CONSUMER_GC_MS = 10 * 60 * 1000


def dump_json(value: Any) -> str:
    """Tham số / kết quả job ghi JSON; UUID, datetime... ghi dạng chuỗi."""
    return json.dumps(value, default=str)


def _now_ms() -> int:
    return int(time.time() * 1000)


# KEYS: job, stream, queued, scheduled. ARGV: key, function, kwargs, retries, timeout,
# scheduled (giây), now (ms).
_ENQUEUE = """
local st = redis.call('HGET', KEYS[1], 'status')
if st and st ~= 'complete' and st ~= 'failed' and st ~= 'aborted' then
  return 0
end
redis.call('DEL', KEYS[1])
local now = tonumber(ARGV[7])
local at = tonumber(ARGV[6])
redis.call('HSET', KEYS[1], 'key', ARGV[1], 'function', ARGV[2], 'kwargs', ARGV[3],
  'retries', ARGV[4], 'timeout', ARGV[5], 'scheduled', ARGV[6], 'queued', ARGV[7], 'attempts', 0)
if at * 1000 > now then
  redis.call('HSET', KEYS[1], 'status', 'scheduled')
  redis.call('ZADD', KEYS[4], at, ARGV[1])
else
  local id = redis.call('XADD', KEYS[2], '*', 'key', ARGV[1])
  redis.call('HSET', KEYS[1], 'status', 'queued', 'entry', id)
  redis.call('ZADD', KEYS[3], now, ARGV[1])
end
return 1
"""

# Job hẹn giờ đã tới giờ → stream. KEYS: scheduled, stream, queued. ARGV: now (giây), tiền tố khoá
# job, số job tối đa.
_SCHEDULE = """
local due = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, tonumber(ARGV[3]))
for _, k in ipairs(due) do
  redis.call('ZREM', KEYS[1], k)
  local h = ARGV[2] .. k
  if redis.call('HGET', h, 'status') == 'scheduled' then
    local id = redis.call('XADD', KEYS[2], '*', 'key', k)
    redis.call('HSET', h, 'status', 'queued', 'entry', id)
    redis.call('ZADD', KEYS[3], tonumber(redis.call('HGET', h, 'scheduled')) * 1000, k)
  end
end
return #due
"""

# Mốc hiện tại (ms) theo đồng hồ Redis, đặt ở đầu các script cần.
_NOW = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
"""

# Worker nhận entry. KEYS: job, queued, active, stream. ARGV: key, entry, worker, group.
_START = (
    _NOW
    + """
if redis.call('HGET', KEYS[1], 'status') ~= 'queued'
    or redis.call('HGET', KEYS[1], 'entry') ~= ARGV[2] then
  redis.call('XACK', KEYS[4], ARGV[4], ARGV[2])
  redis.call('XDEL', KEYS[4], ARGV[2])
  return false
end
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('ZADD', KEYS[3], now, ARGV[1])
redis.call('HINCRBY', KEYS[1], 'attempts', 1)
redis.call('HSET', KEYS[1], 'status', 'active', 'started', now, 'worker', ARGV[3])
return redis.call('HGETALL', KEYS[1])
"""
)

# Báo sống: ghi thông tin + hạn sống của worker, làm mới idle (XCLAIM JUSTID) của entry worker còn
# giữ thật (hash job: `active`, đúng `entry`, đúng worker); trả các entry không còn là của worker.
# KEYS: workers, worker:<id>, stream. ARGV: worker, info, ttl (ms), group, tiền tố khoá job, rồi
# từng cặp entry, khoá job.
_BEAT = (
    _NOW
    + """
redis.call('SET', KEYS[2], ARGV[2], 'PX', ARGV[3])
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[3]), ARGV[1])
local lost = {}
for i = 6, #ARGV, 2 do
  local entry = ARGV[i]
  local cur = redis.call('HMGET', ARGV[5] .. ARGV[i + 1], 'status', 'entry', 'worker')
  if cur[1] == 'active' and cur[2] == entry and cur[3] == ARGV[1] then
    redis.call('XCLAIM', KEYS[3], ARGV[4], ARGV[1], 0, entry, 'JUSTID')
  else
    lost[#lost + 1] = entry
  end
end
return lost
"""
)

# Trả entry: `done` (xong / lỗi hẳn), `retry` (xếp lại cuối stream), `recover` (thu hồi từ worker
# chết: chạy lại nếu còn lượt). KEYS: job, active, queued, stream. ARGV: key, entry, mode, now (ms),
# trạng thái khi `done`, lỗi, kết quả, ttl, group, tính lượt chạy (1/0).
_RELEASE = """
redis.call('XACK', KEYS[4], ARGV[9], ARGV[2])
redis.call('XDEL', KEYS[4], ARGV[2])
if redis.call('HGET', KEYS[1], 'entry') ~= ARGV[2] then
  return 'stale'
end
local st = redis.call('HGET', KEYS[1], 'status')
local mode = ARGV[3]
local status = ARGV[5]
if mode == 'recover' and st == 'queued' then
  local id = redis.call('XADD', KEYS[4], '*', 'key', ARGV[1])
  redis.call('HSET', KEYS[1], 'entry', id)
  return 'requeued'
end
if st ~= 'active' then
  return 'stale'
end
redis.call('ZREM', KEYS[2], ARGV[1])
if ARGV[6] ~= '' then
  redis.call('HSET', KEYS[1], 'error', ARGV[6])
end
if mode == 'recover' then
  local attempts = tonumber(redis.call('HGET', KEYS[1], 'attempts'))
  if attempts < tonumber(redis.call('HGET', KEYS[1], 'retries')) then
    mode = 'retry'
  else
    mode = 'done'
    status = 'failed'
  end
end
if mode == 'retry' then
  if ARGV[10] == '0' then
    redis.call('HINCRBY', KEYS[1], 'attempts', -1)
  end
  local id = redis.call('XADD', KEYS[4], '*', 'key', ARGV[1])
  redis.call('HSET', KEYS[1], 'status', 'queued', 'entry', id, 'queued', ARGV[4], 'scheduled', 0)
  redis.call('ZADD', KEYS[3], ARGV[4], ARGV[1])
  return 'retried'
end
redis.call('HSET', KEYS[1], 'status', status, 'completed', ARGV[4])
if ARGV[7] ~= '' then
  redis.call('HSET', KEYS[1], 'result', ARGV[7])
end
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[8]))
return status
"""


@dataclass(slots=True)
class StreamJob:
    """Một job trong hàng đợi."""

    key: str
    function: str
    kwargs: dict[str, Any] = field(default_factory=dict)
    status: str = "queued"
    attempts: int = 0
    retries: int = 1  # số lần chạy tối đa
    timeout: int = 0  # giây, 0 = không giới hạn
    queued: int = 0  # ms epoch lúc xếp hàng (lần chạy lại: lúc xếp lại)
    scheduled: int = 0  # giây epoch hẹn chạy, 0 = chạy ngay
    started: int = 0  # ms epoch
    completed: int = 0  # ms epoch
    error: str | None = None
    result: Any = None
    worker_id: str | None = None
    entry: str | None = None  # id entry trong stream đang giữ job

    @property
    def id(self) -> str:
        return self.key

    @classmethod
    def from_hash(cls, h: dict[str, str]) -> StreamJob:
        def num(name: str) -> int:
            return int(float(h.get(name) or 0))

        return cls(
            key=h["key"],
            function=h.get("function", ""),
            kwargs=json.loads(h.get("kwargs") or "{}"),
            status=h.get("status", "queued"),
            attempts=num("attempts"),
            retries=num("retries"),
            timeout=num("timeout"),
            queued=num("queued"),
            scheduled=num("scheduled"),
            started=num("started"),
            completed=num("completed"),
            error=h.get("error"),
            result=json.loads(h["result"]) if h.get("result") else None,
            worker_id=h.get("worker"),
            entry=h.get("entry"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "function": self.function,
            "kwargs": self.kwargs,
            "status": self.status,
            "attempts": self.attempts,
            "retries": self.retries,
            "timeout": self.timeout,
            "queued": self.queued,
            "scheduled": self.scheduled,
            "started": self.started,
            "completed": self.completed,
            "error": self.error,
            "result": self.result,
            "worker_id": self.worker_id,
        }


class StreamQueue:
    """Hàng đợi một tên (`jobcrawler-build`, `jobcrawler-poll`, `jobcrawler-boards`): `enqueue`,
    `info`, `job` cho phía xếp việc; các hàm còn lại cho `StreamWorker`."""

    def __init__(self, redis_url: str, name: str, *, reclaim_seconds: float = 60) -> None:
        self.name = name
        self.reclaim_seconds = reclaim_seconds
        self._url = redis_url
        self._redis: Redis | None = None
        p = f"sq:{name}"
        self.prefix = p
        self.stream = f"{p}:stream"
        self._job_prefix = f"{p}:job:"
        self._queued = f"{p}:queued"
        self._active = f"{p}:active"
        self._scheduled = f"{p}:scheduled"
        self._workers = f"{p}:workers"

    def __repr__(self) -> str:
        return f"StreamQueue<{self.name}>"

    @property
    def redis(self) -> Redis:
        if self._redis is None:
            raise RuntimeError(f"{self!r} chưa connect()")
        return self._redis

    def _job_key(self, key: str) -> str:
        return self._job_prefix + key

    def _worker_key(self, worker_id: str) -> str:
        return f"{self.prefix}:worker:{worker_id}"

    async def connect(self) -> None:
        if self._redis is not None:
            return
        self._redis = Redis.from_url(self._url, decode_responses=True)
        self._enqueue = self._redis.register_script(_ENQUEUE)
        self._schedule = self._redis.register_script(_SCHEDULE)
        self._start = self._redis.register_script(_START)
        self._release = self._redis.register_script(_RELEASE)
        self._beat = self._redis.register_script(_BEAT)
        await self.ensure_group()

    async def disconnect(self) -> None:
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    async def ensure_group(self) -> None:
        try:
            await self.redis.xgroup_create(self.stream, GROUP, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def now_ms(self) -> int:
        """Mốc hiện tại (ms) theo đồng hồ Redis (chung cho mọi máy)."""
        seconds, micros = await self.redis.time()
        return int(seconds) * 1000 + int(micros) // 1000

    # --- phía xếp việc ---------------------------------------------------------------------------

    async def enqueue(
        self,
        function: str,
        /,
        *,
        key: str | None = None,
        timeout: float = 0,
        retries: int = 1,
        scheduled: float = 0,
        **kwargs: Any,
    ) -> StreamJob | None:
        """Xếp một job; None nếu job cùng `key` đang hẹn/chờ/chạy."""
        key = key or uuid.uuid4().hex
        now = _now_ms()
        ok = await self._enqueue(
            keys=[self._job_key(key), self.stream, self._queued, self._scheduled],
            # Giờ hẹn giữ phần mili-giây: cắt về giây nguyên thì job hẹn dưới 1 giây có thể vào
            # hàng ngay.
            args=[key, function, dump_json(kwargs), retries, int(timeout), f"{scheduled:.3f}", now],
        )
        if not ok:
            return None
        return StreamJob(
            key=key,
            function=function,
            kwargs=kwargs,
            status="scheduled" if scheduled * 1000 > now else "queued",
            retries=retries,
            timeout=int(timeout),
            queued=now,
            scheduled=int(scheduled),
        )

    async def job(self, key: str) -> StreamJob | None:
        h = await self.redis.hgetall(self._job_key(key))  # pyright: ignore[reportGeneralTypeIssues]
        return StreamJob.from_hash(h) if h else None

    async def info(self, jobs: bool = False, offset: int = 0, limit: int = 10) -> dict[str, Any]:
        """Số job theo trạng thái, runner đang sống, và (nếu `jobs`) một trang job theo thứ tự
        đang chạy → đang chờ → hẹn giờ."""
        r = self.redis
        now = await self.now_ms()
        async with r.pipeline(transaction=False) as pipe:
            pipe.zremrangebyscore(self._workers, "-inf", now)
            pipe.zrange(self._workers, 0, -1)
            pipe.zcard(self._active)
            pipe.zcard(self._queued)
            pipe.zcard(self._scheduled)
            _, worker_ids, active, queued, scheduled = await pipe.execute()
        workers: dict[str, Any] = {}
        if worker_ids:
            raw = await r.mget([self._worker_key(w) for w in worker_ids])
            workers = {w: json.loads(v) for w, v in zip(worker_ids, raw, strict=True) if v}
        listed: list[dict[str, Any]] = []
        if jobs:
            keys: list[str] = []
            skip, want = offset, limit
            for zkey, n in ((self._active, active), (self._queued, queued)):
                if want <= 0:
                    break
                if skip >= n:
                    skip -= n
                    continue
                part = await r.zrange(zkey, skip, skip + want - 1)
                keys += part
                skip, want = 0, want - len(part)
            if want > 0:
                keys += await r.zrange(self._scheduled, skip, skip + want - 1)
            async with r.pipeline(transaction=False) as pipe:
                for k in keys:
                    pipe.hgetall(self._job_key(k))
                hashes = await pipe.execute()
            listed = [StreamJob.from_hash(h).to_dict() for h in hashes if h]
        return {
            "workers": workers,
            "name": self.name,
            "queued": queued,
            "active": active,
            "scheduled": scheduled,
            "jobs": listed,
        }

    async def drop(self) -> int:
        """Xoá mọi khoá của queue (job, stream, group). Chỉ dùng khi không còn worker."""
        r = self.redis
        keys = [k async for k in r.scan_iter(match=f"{self.prefix}:*", count=1000)]
        for i in range(0, len(keys), 1000):
            await r.delete(*keys[i : i + 1000])
        return len(keys)

    # --- phía worker -----------------------------------------------------------------------------

    async def read(self, consumer: str, count: int, block_ms: int) -> list[tuple[str, str | None]]:
        """Entry mới cho `consumer` (tối đa `count`): [(entry, khoá job)]."""
        try:
            resp = await self.redis.xreadgroup(
                GROUP, consumer, {self.stream: ">"}, count=count, block=block_ms
            )
        except ResponseError as exc:
            if "NOGROUP" not in str(exc):
                raise
            await self.ensure_group()  # stream bị xoá (drop): tạo lại
            return []
        return [
            (entry, (fields or {}).get("key"))
            for _stream, entries in resp or []
            for entry, fields in entries
        ]

    async def start(self, key: str | None, entry: str, worker_id: str) -> StreamJob | None:
        """Đánh dấu job đang chạy; None nếu entry đã cũ (job bị xoá / đã xếp lại)."""
        if key is None:
            await self._ack(entry)
            return None
        raw = await self._start(
            keys=[self._job_key(key), self._queued, self._active, self.stream],
            args=[key, entry, worker_id, GROUP],
        )
        if not raw:
            return None
        return StreamJob.from_hash(dict(zip(raw[::2], raw[1::2], strict=True)))

    async def finish(
        self, job: StreamJob, status: str, *, error: str | None = None, result: Any = None
    ) -> str:
        return await self._release_job(
            job.key, job.entry, "done", status=status, error=error, result=result
        )

    async def retry(self, job: StreamJob, error: str, *, count_attempt: bool = True) -> str:
        """Xếp lại cuối stream. `count_attempt=False`: lượt bị ngắt do worker dừng, không tính."""
        return await self._release_job(
            job.key, job.entry, "retry", error=error, count_attempt=count_attempt
        )

    async def _release_job(
        self,
        key: str,
        entry: str | None,
        mode: str,
        *,
        status: str = "",
        error: str | None = None,
        result: Any = None,
        count_attempt: bool = True,
    ) -> str:
        return await self._release(
            keys=[self._job_key(key), self._active, self._queued, self.stream],
            args=[
                key,
                entry or "",
                mode,
                _now_ms(),
                status,
                error or "",
                "" if result is None else dump_json(result),
                JOB_TTL,
                GROUP,
                1 if count_attempt else 0,
            ],
        )

    async def _ack(self, entry: str) -> None:
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.xack(self.stream, GROUP, entry)
            pipe.xdel(self.stream, entry)
            await pipe.execute()

    async def schedule(self) -> int:
        """Chuyển job hẹn giờ đã tới giờ vào stream."""
        moved = await self._schedule(
            keys=[self._scheduled, self.stream, self._queued],
            args=[time.time(), self._job_prefix, SCHEDULE_BATCH],
        )
        return int(moved)

    async def beat(
        self, worker_id: str, info: dict[str, Any], ttl: float, running: Mapping[str, str]
    ) -> list[str]:
        """Báo worker còn sống và làm mới idle các entry nó đang chạy (`running`: entry → khoá
        job) để không bị thu hồi. Trả các entry không còn là của worker (đã bị thu hồi / xếp lại):
        worker phải dừng lượt chạy đó."""
        args: list[Any] = [worker_id, dump_json(info), int(ttl * 1000), GROUP, self._job_prefix]
        for entry, key in running.items():
            args += [entry, key]
        lost = await self._beat(
            keys=[self._workers, self._worker_key(worker_id), self.stream], args=args
        )
        return [str(entry) for entry in lost or []]

    async def leave(self, worker_id: str) -> None:
        r = self.redis
        await r.delete(self._worker_key(worker_id))
        await r.zrem(self._workers, worker_id)
        for c in await r.xinfo_consumers(self.stream, GROUP):
            if c["name"] == worker_id and not c["pending"]:
                await r.xgroup_delconsumer(self.stream, GROUP, worker_id)

    async def reclaim(self, consumer: str) -> Counter[str]:
        """Thu hồi entry idle quá `reclaim_seconds` (worker giữ nó đã chết): chạy lại hoặc
        `failed`. Kèm xoá consumer đã chết khỏi group."""
        r = self.redis
        out: Counter[str] = Counter()
        error = f"worker dừng giữa chừng (không báo sống {self.reclaim_seconds:g} s)"
        start = "0-0"
        while True:
            resp = await r.xautoclaim(
                self.stream,
                GROUP,
                consumer,
                min_idle_time=int(self.reclaim_seconds * 1000),
                start_id=start,
                count=100,
            )
            start, claimed = resp[0], resp[1]
            for entry, fields in claimed:
                key = (fields or {}).get("key")
                if key is None:
                    await self._ack(entry)
                    continue
                out[await self._release_job(key, entry, "recover", error=error)] += 1
            if start in ("0-0", b"0-0"):
                break
        now = await self.now_ms()
        alive = set(await r.zrangebyscore(self._workers, now, "+inf"))
        # Job `active` mà worker đã chết nhưng entry không còn trong PEL (vd. AOF mất đoạn cuối).
        cutoff = now - int(self.reclaim_seconds * 1000)
        for key in await r.zrangebyscore(self._active, "-inf", cutoff):
            fields = ["status", "worker", "entry"]
            status, worker, entry = await r.hmget(self._job_key(key), fields)  # pyright: ignore[reportGeneralTypeIssues]
            if status != "active" or not entry:
                await r.zrem(self._active, key)
            elif worker not in alive:
                out[await self._release_job(key, entry, "recover", error=error)] += 1
        for c in await r.xinfo_consumers(self.stream, GROUP):
            name = c["name"]
            if name not in alive and not c["pending"] and c["idle"] > CONSUMER_GC_MS:
                await r.xgroup_delconsumer(self.stream, GROUP, name)
        return out


@dataclass(slots=True)
class CronJob:
    """Việc định kỳ: xếp với khoá `cron:<tên hàm>` hẹn tới mốc kế tiếp, nên nhiều replica cũng chỉ
    một job mỗi mốc."""

    function: Callable[..., Awaitable[Any]]
    cron: str
    timeout: int | None = None
    retries: int | None = None
    kwargs: dict[str, Any] | None = None
    unique: bool = True


Hook = Callable[[Any], Awaitable[None]]  # nhận `ctx` của worker


class StreamWorker:
    """Chạy job từ một `StreamQueue`: tối đa `concurrency` job cùng lúc, timeout cứng mỗi job,
    chạy lại theo `retries`, cron, báo sống và thu hồi job của worker chết."""

    SIGNALS = (signal.SIGINT, signal.SIGTERM) if os.name != "nt" else ()

    def __init__(
        self,
        queue: StreamQueue,
        *,
        functions: Sequence[Callable[..., Awaitable[Any]] | tuple[str, Callable[..., Any]]],
        concurrency: int = 10,
        cron_jobs: Sequence[CronJob] = (),
        startup: Hook | None = None,
        shutdown: Hook | None = None,
        cancellation_hard_deadline_s: float = 10.0,
        shutdown_grace_s: float = 2.0,
        handle_signals: bool = True,
        tz: tzinfo = UTC,
        on_loop: Callable[[], Awaitable[Any]] | None = None,
        on_iteration: Callable[[], None] | None = None,
    ) -> None:
        self.queue = queue
        self.concurrency = concurrency
        self.cron_jobs = list(cron_jobs)
        self.functions: dict[str, Callable[..., Awaitable[Any]]] = {}
        for f in functions:
            name, fn = f if isinstance(f, tuple) else (f.__name__, f)
            self.functions[name] = fn
        for cj in self.cron_jobs:
            self.functions[cj.function.__name__] = cj.function
        self.startup, self.shutdown = startup, shutdown
        self.cancel_deadline = cancellation_hard_deadline_s
        self.shutdown_grace = shutdown_grace_s
        # Biểu thức cron đọc theo múi giờ này (vd. "*/2 6-23 * * *" là 6h–23h giờ Việt Nam).
        self.tz = tz
        # Nhiều StreamWorker trong một process (runner): process tự bắt tín hiệu rồi gọi stop().
        self._signals = self.SIGNALS if handle_signals else ()
        # Gọi mỗi lần vòng lặp nhận việc quay (ít nhất mỗi BLOCK_MS, kể cả khi hết slot): heartbeat
        # health của process (`jobcrawler.services.redis.heartbeat`) — vòng lặp kẹt thì thôi gọi.
        self.on_loop = on_loop
        # Gọi (đồng bộ) ở đầu MỌI vòng của vòng lặp nhận việc, kể cả khi đọc Redis lỗi: watchdog
        # trong process (`jobcrawler.common.watchdog`) — chỉ vòng lặp đứng hẳn mới làm process
        # thoát.
        self.on_iteration = on_iteration
        self.id = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        self.context: dict[str, Any] = {"worker": self, "queue": queue}
        self.stats: Counter[str] = Counter(complete=0, failed=0, retried=0, aborted=0, revoked=0)
        self.heartbeat = max(1.0, queue.reclaim_seconds / 6)
        self._started = datetime.now(UTC).isoformat()
        self._running: dict[str, asyncio.Task[None]] = {}  # entry → lượt chạy
        self._inner: dict[str, asyncio.Task[Any]] = {}  # entry → hàm job
        self._keys: dict[str, str] = {}  # entry → khoá job (báo sống kiểm quyền giữ)
        self._revoked: set[str] = set()  # entry bị thu hồi khi đang chạy: huỷ, không ghi gì
        self._stop = asyncio.Event()
        self._slot = asyncio.Event()
        self._stopping = False

    def stop(self) -> None:
        self._stop.set()

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        for signum in self._signals:
            loop.add_signal_handler(signum, self.stop)
        log.info("queue.worker_start", queue=self.queue.name, worker=self.id)
        if self.startup is not None:
            await self.startup(self.context)
        upkeep = [
            asyncio.create_task(self._every(self.heartbeat, self._beat)),
            asyncio.create_task(self._every(1.0, self._schedule)),
            asyncio.create_task(self._every(self.queue.reclaim_seconds / 4, self._reclaim)),
        ]
        try:
            await self._fetch()
        finally:
            await self._drain()
            for task in upkeep:
                task.cancel()
            await asyncio.gather(*upkeep, return_exceptions=True)
            with contextlib.suppress(Exception):
                await self.queue.leave(self.id)
            if self.shutdown is not None:
                await self.shutdown(self.context)
            for signum in self._signals:
                loop.remove_signal_handler(signum)

    async def _every(self, seconds: float, fn: Callable[[], Awaitable[Any]]) -> None:
        while not self._stop.is_set():
            try:
                await fn()
            except Exception:
                log.exception("queue.upkeep_error", task=fn.__name__, queue=self.queue.name)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), seconds)

    async def _wait_stop_or(self, event: asyncio.Event, timeout: float | None = None) -> None:
        waiters = [asyncio.create_task(event.wait()), asyncio.create_task(self._stop.wait())]
        _, pending = await asyncio.wait(
            waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()

    async def _looped(self) -> None:
        if self.on_loop is None:
            return
        try:
            await self.on_loop()
        except Exception:
            log.exception("queue.on_loop_error", queue=self.queue.name)

    async def _fetch(self) -> None:
        while not self._stop.is_set():
            if self.on_iteration is not None:
                self.on_iteration()
            free = self.concurrency - len(self._running)
            if free <= 0:
                self._slot.clear()
                # Hết slot: chờ job xong, nhưng quay lại mỗi BLOCK_MS để vẫn báo sống (on_loop).
                await self._wait_stop_or(self._slot, BLOCK_MS / 1000)
                await self._looped()
                continue
            try:
                entries = await self.queue.read(self.id, free, BLOCK_MS)
            except Exception:  # Redis lỗi: không báo sống, healthcheck thấy heartbeat cũ dần
                log.exception("queue.read_error", queue=self.queue.name)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), READ_RETRY_SECONDS)
                continue
            await self._looped()
            for entry, key in entries:
                try:
                    job = await self.queue.start(key, entry, self.id)
                except Exception:  # entry còn trong PEL: thu hồi sau reclaim_seconds
                    log.exception("queue.start_error", queue=self.queue.name, key=key)
                    continue
                if job is not None:
                    self._keys[entry] = job.key
                    self._running[entry] = asyncio.create_task(self._run(entry, job))

    async def _run(self, entry: str, job: StreamJob) -> None:
        try:
            fn = self.functions.get(job.function)
            if fn is None:
                await self._fail(job, f"không có hàm {job.function!r}")
                return
            ctx = {**self.context, "job": job}

            async def call() -> Any:  # sai tham số cũng thành lỗi của job
                return await fn(ctx, **job.kwargs)

            task = asyncio.create_task(call())
            self._inner[entry] = task
            if entry in self._revoked:  # bị thu hồi trước cả khi kịp chạy
                task.cancel()
            done, _ = await asyncio.wait({task}, timeout=job.timeout or None)
            # Từ đây chỉ còn ghi kết quả: báo sống thôi xét entry này (không báo nhầm "bị thu hồi"
            # khi chính mình vừa ghi xong trạng thái job).
            self._keys.pop(entry, None)
            if entry in self._revoked:
                # Lượt đã bị thu hồi (job đã xếp lại / `failed` ở nơi khác): chờ huỷ xong, không
                # ghi gì về job.
                if not done:
                    task.cancel()
                await asyncio.wait({task}, timeout=self.cancel_deadline)
                if task.done() and not task.cancelled():
                    task.exception()  # đã xem: không cảnh báo "exception was never retrieved"
                self.stats["revoked"] += 1
            elif not done:
                task.cancel()
                await asyncio.wait({task}, timeout=self.cancel_deadline)
                await self._fail(job, f"quá timeout {job.timeout} s")
            elif task.cancelled():
                if self._stopping:
                    # Worker dừng (deploy, docker stop): xếp lại, không tính lượt.
                    await self.queue.retry(job, "worker dừng", count_attempt=False)
                    self.stats["aborted"] += 1
                else:
                    await self._fail(job, "bị huỷ")
            elif (exc := task.exception()) is not None:
                await self._fail(job, f"{type(exc).__name__}: {exc}")
            else:
                await self.queue.finish(job, "complete", result=task.result())
                self.stats["complete"] += 1
        except Exception:  # Redis lỗi khi ghi kết quả: entry còn trong PEL, thu hồi sau
            log.exception("queue.job_error", queue=self.queue.name, key=job.key)
        finally:
            self._inner.pop(entry, None)
            self._running.pop(entry, None)
            self._keys.pop(entry, None)
            self._revoked.discard(entry)
            self._slot.set()

    async def _fail(self, job: StreamJob, error: str) -> None:
        if job.attempts < job.retries:
            await self.queue.retry(job, error)
            self.stats["retried"] += 1
            log.warning("queue.job_retry", key=job.key, attempt=job.attempts, error=error)
        else:
            await self.queue.finish(job, "failed", error=error)
            self.stats["failed"] += 1
            log.warning("queue.job_failed", key=job.key, attempt=job.attempts, error=error)

    async def _drain(self) -> None:
        """Dừng: chờ job đang chạy SHUTDOWN_GRACE giây rồi huỷ; job bị huỷ được xếp lại."""
        runs = list(self._running.values())
        if not runs:
            return
        _, pending = await asyncio.wait(runs, timeout=self.shutdown_grace)
        if pending:
            self._stopping = True
            log.info("queue.worker_drain", queue=self.queue.name, cancelled=len(pending))
            for task in list(self._inner.values()):
                task.cancel()
            await asyncio.wait(pending, timeout=self.cancel_deadline + 5)

    async def _beat(self) -> None:
        info = {
            "queue_key": self.queue.name,
            "stats": dict(self.stats),
            "metadata": {
                "concurrency": self.concurrency,
                "active": len(self._running),
                "started": self._started,
            },
        }
        # Hạn báo sống = ngưỡng thu hồi: worker im lặng chừng đó mới bị coi là đã chết.
        lost = await self.queue.beat(self.id, info, self.queue.reclaim_seconds, dict(self._keys))
        for entry in lost:
            if entry not in self._running or entry in self._revoked:
                continue
            # Bị thu hồi khi vẫn đang chạy (event loop từng đứng, mất Redis quá ngưỡng): lượt mới
            # đã / sẽ chạy ở nơi khác, lượt này dừng ngay.
            self._revoked.add(entry)
            log.warning("queue.job_revoked", queue=self.queue.name, key=self._keys.get(entry))
            inner = self._inner.get(entry)
            if inner is not None:
                inner.cancel()

    async def _schedule(self) -> None:
        await self.queue.schedule()
        now = datetime.now(self.tz)
        for cj in self.cron_jobs:
            next_at = croniter(cj.cron, now).get_next(float)
            await self.queue.enqueue(
                cj.function.__name__,
                key=f"cron:{cj.function.__name__}",
                timeout=cj.timeout or 0,
                retries=cj.retries or 1,
                scheduled=next_at,
                **(cj.kwargs or {}),
            )

    async def _reclaim(self) -> None:
        outcome = await self.queue.reclaim(self.id)
        if outcome:
            self.stats["retried"] += outcome.get("retried", 0)
            self.stats["failed"] += outcome.get("failed", 0)
            log.warning("queue.reclaimed", queue=self.queue.name, **dict(outcome))
