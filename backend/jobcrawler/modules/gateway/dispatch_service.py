"""Hàng việc lấy trang trên Redis Streams: backend gửi việc và chờ kết quả, gateway giao việc.

- Mỗi pool / loại việc hai stream (`Keys.streams(pool, "http" | "render")`): stream ưu tiên
  (`:hi`, việc gửi khi `fetch_priority` > 0, tức lượt poll) và stream thường. Gateway giao hết việc
  ưu tiên rồi mới tới việc thường (ưu tiên tuyệt đối: poll chạy ban ngày, build ban đêm). Entry
  `{t: FetchTask protobuf, id: task_id, d: deadline_ms}`; stream chỉ chứa việc chưa giao.
- Giao việc là MỘT script Lua (`_LEASE`): XRANGE lấy entry đầu stream, XDEL, ghi việc vào
  `Keys.inflight(worker)` (task_id → `<deadline_ms>:<leased_ms>`), cộng `Keys.progress(pool, kind)`.
  Không có lúc nào việc đã rời stream mà chưa được ghi là worker đang giữ (gateway chết giữa chừng
  không làm mất việc); mỗi việc giao tối đa một lần. Chưa có việc thì chờ bằng XREAD BLOCK (không
  lấy gì, chỉ để thức dậy) rồi chạy lại script. Entry kiểu cũ (chỉ có `t`) vẫn giao được, phần ghi
  `inflight` của chúng làm ngoài script.
- Complete chỉ nhận kết quả của việc worker còn giữ (HDEL khỏi `inflight`). Worker rời (Leave),
  quá hạn báo sống (prune), hoặc báo sống mà không còn kể việc đã giao quá ORPHAN_GRACE_MS (lời trả
  Lease không tới worker) thì gateway trả `ERROR_KIND_WORKER_LOST` cho việc đó để backend thử lại
  ngay thay vì chờ hết hạn. Việc worker kể là đang giữ mà gateway đã thu hồi thì báo lại để worker
  huỷ (`HeartbeatResponse.revoked_task_ids`).
- Mốc hạn sống / mốc giao việc / `deadline_ms` của việc theo đồng hồ Redis (`TIME`), không theo
  đồng hồ từng gateway / backend. Worker so `deadline_ms` với đồng hồ máy nó (máy worker cần NTP).
- `task_id` = `<client_id>.<seq>`: gateway đọc `client_id` từ đó rồi LPUSH `FetchResult` vào
  `Keys.reply(client_id)` (hết hạn sau REPLY_TTL_SECONDS), nên gateway không giữ trạng thái.
- `FetchClient` (mỗi process backend một cái): XADD việc, một task nền BRPOP list kết quả của
  mình trên kết nối Redis riêng và trả kết quả cho việc đang chờ theo `task_id`. Việc được chờ theo
  từng khung `timeout + FETCH_QUEUE_GRACE_SECONDS`: hết khung mà bộ đếm tiến độ của pool / loại việc
  đã tăng (hàng việc vẫn chạy, chỉ là đông) thì chờ khung tiếp, tổng tối đa
  `timeout + FETCH_QUEUE_MAX_WAIT_SECONDS` (cũng là `deadline_ms` của việc); bộ đếm đứng yên (không
  worker nào lấy việc) thì `NoReplyError`. Thôi chờ thì XDEL việc nếu chưa worker nào lấy.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import math
import os
import re
import secrets
import socket
import time
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Literal

from google.protobuf.message import DecodeError
from redis.asyncio import Redis
from redis.exceptions import RedisError

from jcproto.v1 import crawl_pb2
from jobcrawler.common.logging import get_logger
from jobcrawler.modules.gateway.registry_service import DEFAULT_POOL, Keys, live_capacity
from jobcrawler.services.net.http import TransportError, host_of

if TYPE_CHECKING:
    from jobcrawler.common.config import Settings

log = get_logger(__name__)

Kind = Literal["http", "render"]
KINDS: tuple[Kind, ...] = ("http", "render")
REPLY_TTL_SECONDS = 120  # list kết quả của process backend đã chết tự biến mất
REPLY_BATCH = 100  # sau BRPOP, RPOP thêm tối đa chừng này kết quả một lượt
LIVE_CACHE_SECONDS = 2.0  # cache danh sách worker sống khi định tuyến
LIVE_RETRY_SECONDS = 0.25  # không thấy worker: hỏi lại Redis nếu cache cũ hơn chừng này
# Hash việc worker đang giữ sống quá hạn muộn nhất của các việc trong đó chừng này (worker chạy quá
# timeout tối đa TASK_GUARD_SECONDS = 30 s rồi mới trả kết quả).
INFLIGHT_TTL_MARGIN_MS = 120_000
# Việc đã giao chừng này ms mà worker (vẫn báo sống) không kể là đang giữ: lời trả Lease không tới
# worker. Lớn hơn hạn một lời gọi Heartbeat của worker (15 s) cộng độ trễ mạng.
ORPHAN_GRACE_MS = 60_000
WORKER_LOST_ERROR = "worker đã rời / quá hạn báo sống khi đang giữ việc"
ORPHAN_ERROR = "worker không nhận được việc (lời trả Lease bị mất)"
_TASK_ID = re.compile(r"^([A-Za-z0-9_-]{1,128})\.(\d{1,20})$", re.ASCII)

# Mức ưu tiên của việc lấy trang gửi trong context hiện tại (> 0: stream `:hi`, gateway giao trước).
# Lượt poll đặt 1 (`priority_fetches()`); task asyncio con thừa hưởng giá trị khi được tạo.
fetch_priority: ContextVar[int] = ContextVar("fetch_priority", default=0)


@contextlib.contextmanager
def priority_fetches(level: int = 1) -> Iterator[None]:
    """Việc lấy trang gửi trong khối này (kể cả task con tạo trong đó) đi stream ưu tiên."""
    token = fetch_priority.set(level)
    try:
        yield
    finally:
        fetch_priority.reset(token)


class DispatchError(TransportError):
    """Lỗi của hàng việc (không có worker, worker không trả kết quả), không do proxy hay trang."""


class NoWorkerError(DispatchError):
    """Không có worker nào sống cho pool / loại việc: báo lỗi ngay, không xếp hàng chờ."""

    def __init__(self, message: str) -> None:
        super().__init__("other", f"no_worker: {message}")


class NoReplyError(DispatchError):
    """Không worker nào trả kết quả: hết một khung chờ mà hàng việc đứng yên, hoặc hết
    `timeout + FETCH_QUEUE_MAX_WAIT_SECONDS`."""

    def __init__(self, message: str) -> None:
        super().__init__("timeout", message)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def make_client_id() -> str:
    """`<hostname>-<pid>-<ngẫu nhiên>`, không có dấu chấm (dấu chấm ngăn với số thứ tự)."""
    host = re.sub(r"[^A-Za-z0-9_-]+", "-", socket.gethostname()).strip("-")[:40] or "host"
    return f"{host}-{os.getpid()}-{secrets.token_hex(4)}"


def parse_task_id(task_id: str) -> str | None:
    """`<client_id>.<seq>` → `client_id`; sai dạng → None."""
    m = _TASK_ID.match(task_id)
    return m.group(1) if m else None


def task_kind(task: crawl_pb2.FetchTask) -> Kind:
    return "render" if task.kind == crawl_pb2.TASK_KIND_RENDER else "http"


def match_route(host: str, routes: Mapping[str, str]) -> str | None:
    """Pool cho `host` theo FETCH_ROUTES (khớp đúng host hoặc subdomain; hậu tố dài nhất thắng)."""
    host = host.lower().rstrip(".")
    best: tuple[int, str] | None = None
    for suffix, pool in routes.items():
        suffix = suffix.strip().lower().strip(".")
        if not suffix or not pool:
            continue
        if (host == suffix or host.endswith("." + suffix)) and (
            best is None or len(suffix) > best[0]
        ):
            best = (len(suffix), pool)
    return best[1] if best else None


def choose_pool(
    host: str, kind: Kind, routes: Mapping[str, str], live: Mapping[str, Mapping[str, int]]
) -> str:
    """Pool theo route nếu có worker sống làm được `kind`, không thì `default`; không pool nào có
    worker → `NoWorkerError`."""
    routed = match_route(host, routes) or DEFAULT_POOL
    for pool in dict.fromkeys((routed, DEFAULT_POOL)):
        if live.get(pool, {}).get(kind, 0) > 0:
            return pool
    where = routed if routed == DEFAULT_POOL else f"{routed}, {DEFAULT_POOL}"
    raise NoWorkerError(f"không có worker {kind} nào đang sống (pool {where})")


# --- phía gateway --------------------------------------------------------------------------------

# Mốc hiện tại (ms) theo đồng hồ Redis, đặt ở đầu các script cần.
_NOW = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
"""

# Giao tối đa ARGV[1] việc đầu stream cho một worker, nguyên tử. KEYS: stream, inflight, progress.
# ARGV: số việc, lề TTL của inflight (ms). Trả {số entry đã lấy, số việc quá hạn, blob, cờ, ...};
# cờ '1' = đã ghi inflight, '0' = entry kiểu cũ (không có `id`): gateway tự ghi.
_LEASE = (
    _NOW
    + """
local entries = redis.call('XRANGE', KEYS[1], '-', '+', 'COUNT', tonumber(ARGV[1]))
if #entries == 0 then
  return {0, 0}
end
local out = {#entries, 0}
local ids = {}
local latest = -1
for _, e in ipairs(entries) do
  ids[#ids + 1] = e[1]
  local f = e[2]
  local blob, id, d = nil, nil, 0
  for i = 1, #f, 2 do
    if f[i] == 't' then blob = f[i + 1]
    elseif f[i] == 'id' then id = f[i + 1]
    elseif f[i] == 'd' then d = tonumber(f[i + 1]) or 0 end
  end
  if blob then
    if d > 0 and d <= now then
      out[2] = out[2] + 1
    elseif id then
      redis.call('HSET', KEYS[2], id, d .. ':' .. now)
      if d > latest then latest = d end
      out[#out + 1] = blob
      out[#out + 1] = '1'
    else
      out[#out + 1] = blob
      out[#out + 1] = '0'
    end
  end
end
redis.call('XDEL', KEYS[1], unpack(ids))
redis.call('INCRBY', KEYS[3], #entries)
if latest >= 0 then
  -- Chỉ kéo dài, không rút ngắn hạn của hash (việc giao trước có thể hạn muộn hơn).
  local at = math.max(now, latest) + tonumber(ARGV[2])
  redis.call('PEXPIREAT', KEYS[2], at, 'NX')
  redis.call('PEXPIREAT', KEYS[2], at, 'GT')
end
return out
"""
)

# Prune một worker: chỉ khi nó VẪN quá hạn báo sống (có thể vừa Register lại). KEYS: workers,
# inflight. ARGV: worker_id. Trả nil nếu worker còn sống, không thì [task_id, giá trị, ...] việc
# nó còn giữ (đã xoá khỏi inflight, worker đã bỏ khỏi danh sách sống).
_PRUNE_ONE = (
    _NOW
    + """
local score = redis.call('ZSCORE', KEYS[1], ARGV[1])
if score and tonumber(score) > now then
  return false
end
redis.call('ZREM', KEYS[1], ARGV[1])
local held = redis.call('HGETALL', KEYS[2])
redis.call('DEL', KEYS[2])
return held
"""
)

# Đối soát việc worker báo đang giữ (Heartbeat) với `inflight`. KEYS: inflight. ARGV: grace (ms),
# rồi task_id worker kể. Việc trong inflight mà worker không kể, đã giao quá grace → xoá, trả về
# (worker không có việc đó). Việc worker kể mà inflight không có → gateway đã thu hồi: worker huỷ.
# Trả {{task_id, giá trị, ...} mất, {task_id, ...} thu hồi}.
_RECONCILE = (
    _NOW
    + """
local grace = tonumber(ARGV[1])
local reported = {}
local revoked = {}
for i = 2, #ARGV do
  reported[ARGV[i]] = true
  if redis.call('HEXISTS', KEYS[1], ARGV[i]) == 0 then
    revoked[#revoked + 1] = ARGV[i]
  end
end
local lost = {}
local held = redis.call('HGETALL', KEYS[1])
for i = 1, #held, 2 do
  local id, value = held[i], held[i + 1]
  if not reported[id] then
    local leased = tonumber(string.match(value, ':(%d+)$') or '')
    if leased and leased + grace <= now then
      redis.call('HDEL', KEYS[1], id)
      lost[#lost + 1] = id
      lost[#lost + 1] = value
    end
  end
end
return {lost, revoked}
"""
)


def _deadline_of(value: Any) -> int:
    """Giá trị trong `inflight`: `<deadline_ms>:<leased_ms>` (bản cũ: `<deadline_ms>`)."""
    try:
        return int(_text(value).split(":", 1)[0] or 0)
    except ValueError:
        return 0


async def redis_now_ms(redis: Redis) -> int:
    """Mốc hiện tại (ms) theo đồng hồ Redis (chung cho mọi gateway / backend)."""
    seconds, micros = await redis.time()
    return int(seconds) * 1000 + int(micros) // 1000


class _Lease:
    """Một lần Lease của worker `consumer`: lấy việc theo thứ tự ưu tiên tới khi hết slot."""

    def __init__(
        self, redis: Redis, keys: Keys, pool: str, consumer: str, slots: dict[Kind, int]
    ) -> None:
        self.redis = redis
        self.keys = keys
        self.pool = pool
        self.consumer = consumer
        self.slots = slots
        # render trước http (như trước đây); mỗi loại: stream ưu tiên trước stream thường.
        self.lanes: dict[Kind, tuple[str, str]] = {k: keys.streams(pool, k) for k in slots}
        self.tasks: list[crawl_pb2.FetchTask] = []

    def open_streams(self) -> list[str]:
        return [s for kind, lane in self.lanes.items() if self.slots[kind] > 0 for s in lane]

    async def take(self, kind: Kind, stream: str) -> bool:
        """Một lần `_LEASE` trên `stream` cho tối đa `slots[kind]` việc. Việc quá `deadline_ms`
        bị bỏ. Trả True nếu đã lấy được entry nào khỏi stream."""
        inflight = self.keys.inflight(self.consumer)
        resp: Any = await self.redis.eval(  # pyright: ignore[reportGeneralTypeIssues]
            _LEASE,
            3,
            stream,
            inflight,
            self.keys.progress(self.pool, kind),
            self.slots[kind],
            INFLIGHT_TTL_MARGIN_MS,
        )
        consumed, expired = int(resp[0]), int(resp[1])
        if not consumed:
            return False
        legacy: dict[str, str] = {}
        latest = now = _now_ms()
        for blob, recorded in zip(resp[2::2], resp[3::2], strict=True):
            try:
                task = crawl_pb2.FetchTask.FromString(blob)
            except DecodeError:
                log.warning("gateway.bad_task", stream=stream)
                continue
            if _text(recorded) != "1":  # entry kiểu cũ: hạn nằm trong protobuf
                if task.deadline_ms and task.deadline_ms <= now:
                    expired += 1
                    continue
                legacy[task.task_id] = f"{task.deadline_ms}:{now}"
                latest = max(latest, task.deadline_ms)
            self.tasks.append(task)
            self.slots[kind] -= 1
        if legacy:
            expire_at = latest + INFLIGHT_TTL_MARGIN_MS
            async with self.redis.pipeline(transaction=False) as pipe:
                pipe.hset(inflight, mapping=legacy)
                pipe.pexpireat(inflight, expire_at, nx=True)
                pipe.pexpireat(inflight, expire_at, gt=True)
                await pipe.execute()
        if expired:
            log.info("gateway.expired", consumer=self.consumer, tasks=expired)
        return True

    async def top_up(self) -> None:
        """Lấy (không chờ) cho đủ slot: mỗi loại việc hết stream ưu tiên rồi tới stream thường."""
        for kind, lane in self.lanes.items():
            for stream in lane:
                while self.slots[kind] > 0 and await self.take(kind, stream):
                    pass


async def lease_tasks(
    redis: Redis,
    keys: Keys,
    pool: str,
    consumer: str,
    *,
    http: int,
    render: int,
    wait_ms: int,
) -> list[crawl_pb2.FetchTask]:
    """Lấy tối đa `render` việc render và `http` việc HTTP của `pool` cho worker `consumer`.

    Lấy ngay (không chờ) trước, mỗi loại việc stream ưu tiên trước; chưa có việc nào thì chờ tối
    đa `wait_ms` tới khi một stream còn slot có entry rồi lấy lại. Việc đã quá `deadline_ms` bị bỏ
    (không giao). Việc giao được ghi vào `Keys.inflight(consumer)` cùng lúc rời stream.
    """
    wanted: tuple[tuple[Kind, int], ...] = (("render", render), ("http", http))
    slots: dict[Kind, int] = {kind: n for kind, n in wanted if n > 0}
    if not slots:
        return []
    lease = _Lease(redis, keys, pool, consumer, slots)
    deadline = time.monotonic() + max(0, wait_ms) / 1000
    await lease.top_up()
    while not lease.tasks:
        left_ms = int((deadline - time.monotonic()) * 1000)
        streams = lease.open_streams()
        if left_ms < 1 or not streams:
            break
        # Chờ tới khi một stream có entry (id 0-0: stream đang có entry thì trả ngay; BLOCK 0 là
        # chờ mãi nên luôn ≥ 1 ms). XREAD không lấy gì: việc lấy bằng `_LEASE` theo thứ tự ưu tiên.
        await redis.xread(dict.fromkeys(streams, "0-0"), count=1, block=max(1, left_ms))
        await lease.top_up()
    return lease.tasks


async def push_results(
    redis: Redis, keys: Keys, results: Iterable[crawl_pb2.FetchResult]
) -> tuple[int, int]:
    """Trả kết quả về list của process backend ghi trong `task_id`. Trả (nhận, bỏ)."""
    accepted = dropped = 0
    async with redis.pipeline(transaction=False) as pipe:
        for result in results:
            client_id = parse_task_id(result.task_id)
            if client_id is None:
                dropped += 1
                continue
            key = keys.reply(client_id)
            pipe.lpush(key, result.SerializeToString())
            pipe.expire(key, REPLY_TTL_SECONDS)
            accepted += 1
        if accepted:
            await pipe.execute()
    return accepted, dropped


async def complete_results(
    redis: Redis, keys: Keys, worker_id: str, results: Iterable[crawl_pb2.FetchResult]
) -> tuple[int, int, int]:
    """Nhận kết quả của việc `worker_id` còn giữ (HDEL khỏi `inflight`) và trả về backend; bỏ kết
    quả có `task_id` sai dạng và kết quả của việc worker không còn giữ (đã trả `worker_lost` cho
    backend, hoặc chưa từng giao cho worker này). Trả (nhận, sai dạng, không còn giữ)."""
    results = list(results)
    valid = [r for r in results if parse_task_id(r.task_id) is not None]
    malformed = len(results) - len(valid)
    if not valid:
        return 0, malformed, 0
    key = keys.inflight(worker_id)
    async with redis.pipeline(transaction=False) as pipe:
        for result in valid:
            pipe.hdel(key, result.task_id)
        removed = await pipe.execute()
    kept = [result for result, n in zip(valid, removed, strict=True) if n]
    accepted, _ = await push_results(redis, keys, kept)
    return accepted, malformed, len(valid) - len(kept)


async def _report_lost(
    redis: Redis, keys: Keys, worker_id: str, held: Mapping[Any, Any], error: str
) -> int:
    """Trả `ERROR_KIND_WORKER_LOST` cho các việc `held` (task_id → giá trị inflight) để backend
    thử lại ngay; việc đã quá hạn (backend thôi chờ) thì không trả. Trả số việc đã trả."""
    now = await redis_now_ms(redis)
    lost = [
        crawl_pb2.FetchResult(
            task_id=_text(task_id),
            error_kind=crawl_pb2.ERROR_KIND_WORKER_LOST,
            error=error,
            worker_id=worker_id,
        )
        for task_id, value in held.items()
        if not (deadline := _deadline_of(value)) or deadline > now
    ]
    if not lost:
        return 0
    accepted, _ = await push_results(redis, keys, lost)
    return accepted


def _pairs(flat: Any) -> dict[Any, Any]:
    items = list(flat or [])
    return dict(zip(items[::2], items[1::2], strict=True))


async def release_worker(redis: Redis, keys: Keys, worker_id: str) -> int:
    """Worker rời: trả `ERROR_KIND_WORKER_LOST` cho mọi việc nó còn giữ (backend thử lại ngay) rồi
    xoá danh sách đó. HGETALL + DEL trong một transaction: Complete chạy song song hoặc nhận kết quả
    trước (việc không còn trong danh sách), hoặc bị bỏ. Trả số việc đã trả về backend."""
    key = keys.inflight(worker_id)
    async with redis.pipeline(transaction=True) as pipe:
        pipe.hgetall(key)
        pipe.delete(key)
        held, _ = await pipe.execute()
    return await _report_lost(redis, keys, worker_id, held or {}, WORKER_LOST_ERROR)


async def release_expired_worker(redis: Redis, keys: Keys, worker_id: str) -> int | None:
    """Prune một worker quá hạn báo sống: bỏ khỏi danh sách sống và trả `worker_lost` cho việc nó
    còn giữ — chỉ khi nó VẪN quá hạn (một script Lua: worker vừa Register lại thì giữ nguyên, việc
    mới giao cho nó không bị trả oan). Trả None nếu worker còn sống, không thì số việc đã trả."""
    held = await redis.eval(  # pyright: ignore[reportGeneralTypeIssues]
        _PRUNE_ONE, 2, keys.workers, keys.inflight(worker_id), worker_id
    )
    if held is None:
        return None
    return await _report_lost(redis, keys, worker_id, _pairs(held), WORKER_LOST_ERROR)


async def reconcile_worker(
    redis: Redis,
    keys: Keys,
    worker_id: str,
    reported: Iterable[str],
    *,
    grace_ms: int | None = None,
) -> tuple[int, list[str]]:
    """Đối soát việc worker kể là đang giữ (Heartbeat) với `inflight`: việc đã giao quá `grace_ms`
    mà worker không kể (lời trả Lease không tới) → trả `worker_lost` cho backend; việc worker kể mà
    gateway không còn ghi nhận → worker phải huỷ. Trả (số việc đã trả về backend, task_id thu
    hồi)."""
    grace = ORPHAN_GRACE_MS if grace_ms is None else grace_ms
    lost, revoked = await redis.eval(  # pyright: ignore[reportGeneralTypeIssues]
        _RECONCILE, 1, keys.inflight(worker_id), grace, *reported
    )
    returned = await _report_lost(redis, keys, worker_id, _pairs(lost), ORPHAN_ERROR)
    return returned, [_text(t) for t in revoked or []]


# --- phía backend --------------------------------------------------------------------------------


def _clone(redis: Redis) -> Redis:
    """Client Redis riêng (pool riêng, cùng tham số) cho lệnh chặn BRPOP."""
    pool = redis.connection_pool
    return Redis.from_pool(
        type(pool)(connection_class=pool.connection_class, **pool.connection_kwargs)
    )


class FetchClient:
    """Gửi `FetchTask` tới worker (qua stream của pool) và chờ `FetchResult`.

    `submit()` định tuyến theo FETCH_ROUTES (pool không có worker sống thì về `default`), gán
    `task_id` + `deadline_ms` + `priority` (theo `fetch_priority`), XADD, rồi chờ theo từng khung
    `timeout + FETCH_QUEUE_GRACE_SECONDS`, kéo dài khi hàng việc còn chạy, tối đa
    `timeout + FETCH_QUEUE_MAX_WAIT_SECONDS`.
    """

    def __init__(
        self,
        redis: Redis,
        settings: Settings,
        *,
        client_id: str | None = None,
    ) -> None:
        self.redis = redis
        self.keys = Keys(settings.redis_prefix)
        self.client_id = client_id or make_client_id()
        if parse_task_id(f"{self.client_id}.1") != self.client_id:
            raise ValueError(f"client_id không hợp lệ: {self.client_id!r}")
        self.reply_key = self.keys.reply(self.client_id)
        self.routes = dict(settings.fetch_routes)
        self.grace = settings.fetch_queue_grace_seconds
        self.max_wait = max(settings.fetch_queue_max_wait_seconds, self.grace)
        self.maxlen = settings.fetch_stream_maxlen
        self.max_body_bytes = settings.fetch_max_body_bytes
        self._blocking = _clone(redis)
        self._pending: dict[str, asyncio.Future[crawl_pb2.FetchResult]] = {}
        self._seq = itertools.count(1)
        self._reader: asyncio.Task[None] | None = None
        self._cleanup: set[asyncio.Task[None]] = set()
        self._closed = False
        self._live: dict[str, dict[str, int]] = {}
        self._live_at = -math.inf
        self._live_lock = asyncio.Lock()
        self.counters: Counter[str] = Counter(
            submitted=0, completed=0, timeouts=0, no_worker=0, discarded=0, extended=0
        )

    def stats(self) -> dict[str, int]:
        """Số việc đã gửi / có kết quả / hết hạn chờ / không có worker / kết quả bị bỏ (muộn,
        không rõ việc) / số lần chờ thêm một khung vì hàng việc còn chạy trong process này, và số
        việc đang chờ."""
        return {**self.counters, "inflight": len(self._pending)}

    async def _live_capacity(self, max_age: float) -> dict[str, dict[str, int]]:
        async with self._live_lock:
            if time.monotonic() - self._live_at >= max_age:
                try:
                    self._live = await live_capacity(self.redis, self.keys)
                except RedisError as exc:
                    raise DispatchError("other", f"redis: {exc}") from exc
                self._live_at = time.monotonic()
            return self._live

    async def pool_for(self, url: str, kind: Kind) -> str:
        host = host_of(url)
        live = await self._live_capacity(LIVE_CACHE_SECONDS)
        try:
            return choose_pool(host, kind, self.routes, live)
        except NoWorkerError:
            # Worker có thể vừa đăng ký: hỏi lại Redis (tối đa mỗi LIVE_RETRY_SECONDS).
            live = await self._live_capacity(LIVE_RETRY_SECONDS)
            try:
                return choose_pool(host, kind, self.routes, live)
            except NoWorkerError:
                self.counters["no_worker"] += 1
                raise

    def _start(self) -> None:
        if self._reader is None or self._reader.done():
            self._reader = asyncio.get_running_loop().create_task(
                self._read_replies(), name=f"fetch-replies:{self.client_id}"
            )

    async def submit(self, task: crawl_pb2.FetchTask) -> crawl_pb2.FetchResult:
        """Gửi `task` (gán `task_id`, `deadline_ms`, `priority`) và chờ kết quả.

        Lỗi: `NoWorkerError` (không có worker sống), `NoReplyError` (hết hạn chờ),
        `DispatchError` (Redis lỗi, client đã đóng)."""
        if self._closed:
            raise DispatchError("other", "FetchClient đã đóng")
        self._start()
        kind = task_kind(task)
        pool = await self.pool_for(task.url, kind)
        if task.priority <= 0:
            task.priority = max(0, fetch_priority.get())
        stream = self.keys.stream(pool, kind, high=task.priority > 0)
        progress = self.keys.progress(pool, kind)
        timeout = max(0.0, task.timeout_seconds)
        task.task_id = f"{self.client_id}.{next(self._seq)}"
        future: asyncio.Future[crawl_pb2.FetchResult] = asyncio.get_running_loop().create_future()
        self._pending[task.task_id] = future
        entry_id: Any = None
        answered = False
        try:
            try:
                # Worker bỏ việc quá mốc này: đúng lúc backend thôi chờ hẳn. Tính theo đồng hồ Redis
                # như gateway khi giao việc / trả `worker_lost` (backend lệch giờ không làm việc bị
                # bỏ oan); worker so với đồng hồ máy nó.
                wait_ms = int((timeout + self.max_wait) * 1000)
                task.deadline_ms = await redis_now_ms(self.redis) + wait_ms
                async with self.redis.pipeline(transaction=False) as pipe:
                    pipe.get(progress)
                    pipe.xadd(
                        stream,
                        # `id` / `d` để gateway ghi việc đang giữ ngay trong script giao việc.
                        {"t": task.SerializeToString(), "id": task.task_id, "d": task.deadline_ms},
                        maxlen=self.maxlen,
                        approximate=True,
                    )
                    seen, entry_id = await pipe.execute()
            except RedisError as exc:
                raise DispatchError("other", f"redis: {exc}") from exc
            self.counters["submitted"] += 1
            started = time.monotonic()
            result = await self._wait(future, progress, seen, timeout=timeout)
            if result is None:
                self.counters["timeouts"] += 1
                waited = time.monotonic() - started
                log.debug("fetch.no_reply", url=task.url, pool=pool, kind=kind, waited=waited)
                raise NoReplyError(
                    f"không có worker trả kết quả trong {waited:.0f} s (pool {pool}, {kind})"
                )
            answered = True
            self.counters["completed"] += 1
            return result
        finally:
            self._pending.pop(task.task_id, None)
            if not answered and entry_id is not None and not self._closed:
                self._forget(stream, entry_id)

    async def _wait(
        self,
        future: asyncio.Future[crawl_pb2.FetchResult],
        progress: str,
        seen: Any,
        *,
        timeout: float,
    ) -> crawl_pb2.FetchResult | None:
        """Chờ `future` theo từng khung `timeout + grace` giây. Hết khung mà bộ đếm tiến độ
        `progress` đã đổi (gateway vẫn đang giao việc của pool / loại việc này) thì chờ khung tiếp,
        tổng tối đa `timeout + max_wait` giây. Trả None khi thôi chờ."""
        window = timeout + self.grace
        started = time.monotonic()
        hard = started + timeout + self.max_wait
        end = min(started + window, hard)
        while True:
            # `asyncio.wait` không huỷ `future` khi hết giờ: khung sau chờ tiếp được.
            done, _ = await asyncio.wait({future}, timeout=max(0.0, end - time.monotonic()))
            if done:
                return future.result()
            now = time.monotonic()
            if now >= hard:
                return None
            try:
                current = await self.redis.get(progress)
            except RedisError:
                return None
            if current is None or current == seen:
                return None
            seen = current
            self.counters["extended"] += 1
            end = min(now + window, hard)

    def _forget(self, stream: str, entry_id: Any) -> None:
        """Thôi chờ (hết hạn / bị huỷ): XDEL việc nếu còn trong stream (chưa worker nào lấy) để
        worker không tải thừa. Chạy nền: không làm chậm lỗi trả về hay việc huỷ."""

        async def xdel() -> None:
            with contextlib.suppress(RedisError, OSError):
                await self.redis.xdel(stream, entry_id)

        job = asyncio.get_running_loop().create_task(xdel())
        self._cleanup.add(job)
        job.add_done_callback(self._cleanup.discard)

    def _resolve(self, blob: bytes) -> None:
        try:
            result = crawl_pb2.FetchResult.FromString(blob)
        except DecodeError:
            self.counters["discarded"] += 1
            return
        future = self._pending.get(result.task_id)
        if future is None or future.done():
            self.counters["discarded"] += 1  # việc đã hết hạn chờ / không phải của process này
            return
        future.set_result(result)

    async def _read_replies(self) -> None:
        redis = self._blocking
        while True:
            try:
                item: Any = await redis.brpop([self.reply_key], timeout=1)  # pyright: ignore[reportGeneralTypeIssues]
                if item is None:
                    continue
                blobs: list[bytes] = [item[1]]
                more: Any = await redis.rpop(self.reply_key, REPLY_BATCH)  # pyright: ignore[reportGeneralTypeIssues]
                if more:
                    blobs.extend(more)
            except (RedisError, OSError) as exc:
                log.warning("fetch.reply_error", error=repr(exc))
                await asyncio.sleep(1)
                continue
            for blob in blobs:
                self._resolve(blob)

    async def close(self) -> None:
        """Dừng đọc kết quả; việc đang chờ nhận `DispatchError`."""
        if self._closed:
            return
        self._closed = True
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
        for future in self._pending.values():
            if not future.done():
                future.set_exception(DispatchError("other", "FetchClient đã đóng"))
        if self._cleanup:
            await asyncio.gather(*self._cleanup, return_exceptions=True)
        await self._blocking.aclose()
