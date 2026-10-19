"""Hàng việc lấy trang trên Redis Streams: backend gửi việc và chờ kết quả, gateway giao việc.

- Mỗi pool / loại việc hai stream (`Keys.streams(pool, "http" | "render")`): stream ưu tiên
  (`:hi`, việc gửi khi `fetch_priority` > 0, tức lượt poll) và stream thường. Gateway giao hết việc
  ưu tiên rồi mới tới việc thường (ưu tiên tuyệt đối: poll chạy ban ngày, build ban đêm). Entry
  `{t: FetchTask protobuf}`, consumer group `workers`, mỗi worker là một consumer. Gateway đọc hộ
  worker bằng XREADGROUP NOACK rồi XDEL ngay: mỗi việc giao tối đa một lần.
- Cùng lượt XDEL, gateway ghi việc đã giao vào `Keys.inflight(worker)` (task_id → deadline_ms) và
  cộng số entry đã lấy vào `Keys.progress(pool, kind)`. Complete chỉ nhận kết quả của việc worker
  còn giữ; worker rời (Leave) hoặc quá hạn báo sống (prune) thì gateway trả `ERROR_KIND_WORKER_LOST`
  cho từng việc nó còn giữ để backend thử lại ngay thay vì chờ hết hạn.
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
from redis.exceptions import RedisError, ResponseError

from jcproto.v1 import crawl_pb2
from jobcrawler.gateway.registry import DEFAULT_POOL, Keys, live_capacity
from jobcrawler.logging import get_logger
from jobcrawler.net.http import TransportError, host_of

if TYPE_CHECKING:
    from jobcrawler.config import Settings

log = get_logger(__name__)

Kind = Literal["http", "render"]
KINDS: tuple[Kind, ...] = ("http", "render")
GROUP = "workers"
REPLY_TTL_SECONDS = 120  # list kết quả của process backend đã chết tự biến mất
REPLY_BATCH = 100  # sau BRPOP, RPOP thêm tối đa chừng này kết quả một lượt
LIVE_CACHE_SECONDS = 2.0  # cache danh sách worker sống khi định tuyến
LIVE_RETRY_SECONDS = 0.25  # không thấy worker: hỏi lại Redis nếu cache cũ hơn chừng này
# Hash việc worker đang giữ sống quá hạn muộn nhất của các việc trong đó chừng này (worker chạy quá
# timeout tối đa TASK_GUARD_SECONDS = 30 s rồi mới trả kết quả).
INFLIGHT_TTL_MARGIN_MS = 120_000
WORKER_LOST_ERROR = "worker đã rời / quá hạn báo sống khi đang giữ việc"
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


async def ensure_group(redis: Redis, stream: str) -> None:
    """Tạo consumer group (kèm stream) nếu chưa có; id `0` để việc xếp trước đó vẫn được giao."""
    try:
        await redis.xgroup_create(stream, GROUP, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def ensure_groups(redis: Redis, keys: Keys, pool: str) -> None:
    """Consumer group cho mọi stream của pool (http / render, ưu tiên / thường)."""
    for kind in KINDS:
        for stream in keys.streams(pool, kind):
            await ensure_group(redis, stream)


async def delete_consumer(redis: Redis, keys: Keys, pool: str, consumer: str) -> None:
    """Bỏ consumer của worker đã rời (NOACK nên không còn entry nào đang giữ)."""
    for kind in KINDS:
        for stream in keys.streams(pool, kind):
            with contextlib.suppress(ResponseError):  # NOGROUP: stream đã bị xoá
                await redis.xgroup_delconsumer(stream, GROUP, consumer)


def _entries(resp: Any) -> list[tuple[str, list[tuple[Any, Any]]]]:
    """Kết quả XREADGROUP (RESP2: list, RESP3: dict) → [(stream, [(id, fields)])]."""
    if not resp:
        return []
    out: list[tuple[str, list[tuple[Any, Any]]]] = []
    items = resp.items() if isinstance(resp, dict) else resp
    for stream, entries in items:
        if entries and isinstance(entries[0], list):  # RESP3: [[(id, fields), ...]]
            entries = entries[0]
        out.append((_text(stream), list(entries or [])))
    return out


class _Lease:
    """Một lần Lease của worker `consumer`: đọc stream theo thứ tự ưu tiên tới khi hết slot."""

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
        self.kind_of: dict[str, Kind] = {s: k for k, lane in self.lanes.items() for s in lane}
        self.tasks: list[crawl_pb2.FetchTask] = []

    def open_streams(self) -> list[str]:
        return [s for kind, lane in self.lanes.items() if self.slots[kind] > 0 for s in lane]

    async def read(self, want: Mapping[str, int], *, block_ms: int | None) -> bool:
        """Một lần XREADGROUP NOACK (`want`: stream → số entry tối đa; nhiều stream thì COUNT là số
        nhỏ nhất), rồi một pipeline: XDEL entry đã đọc, ghi việc giao vào `inflight` của worker,
        cộng bộ đếm tiến độ. Việc quá `deadline_ms` bị bỏ. Trả True nếu đã lấy được entry nào."""
        try:
            resp = await self.redis.xreadgroup(
                GROUP,
                self.consumer,
                dict.fromkeys(want, ">"),
                count=min(want.values()),
                block=block_ms,
                noack=True,
            )
        except ResponseError as exc:
            if "NOGROUP" not in str(exc):
                raise
            for stream in want:
                await ensure_group(self.redis, stream)
            return False
        now = _now_ms()
        expired = 0
        held: dict[str, int] = {}
        consumed: Counter[Kind] = Counter()
        async with self.redis.pipeline(transaction=False) as pipe:
            for stream, entries in _entries(resp):
                if not entries:
                    continue
                kind = self.kind_of[stream]
                consumed[kind] += len(entries)
                pipe.xdel(stream, *[entry_id for entry_id, _ in entries])
                for _, fields in entries:
                    blob = (fields or {}).get(b"t") or (fields or {}).get("t")
                    if not blob:
                        continue
                    try:
                        task = crawl_pb2.FetchTask.FromString(blob)
                    except DecodeError:
                        log.warning("gateway.bad_task", stream=stream)
                        continue
                    if task.deadline_ms and task.deadline_ms <= now:
                        expired += 1
                        continue
                    self.tasks.append(task)
                    self.slots[kind] -= 1
                    held[task.task_id] = task.deadline_ms
            if not consumed:
                return False
            if held:
                key = self.keys.inflight(self.consumer)
                pipe.hset(key, mapping=held)
                # Chỉ kéo dài, không rút ngắn hạn của hash (việc giao trước có thể hạn muộn hơn).
                expire_at = max(now, *held.values()) + INFLIGHT_TTL_MARGIN_MS
                pipe.pexpireat(key, expire_at, nx=True)
                pipe.pexpireat(key, expire_at, gt=True)
            for kind, n in consumed.items():
                pipe.incrby(self.keys.progress(self.pool, kind), n)
            await pipe.execute()
        if expired:
            log.info("gateway.expired", consumer=self.consumer, tasks=expired)
        return True

    async def top_up(self) -> None:
        """Lấy (không chờ) cho đủ slot: mỗi loại việc hết stream ưu tiên rồi tới stream thường."""
        for kind, lane in self.lanes.items():
            for stream in lane:
                while self.slots[kind] > 0 and await self.read(
                    {stream: self.slots[kind]}, block_ms=None
                ):
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

    Đọc ngay (không chờ) trước, mỗi loại việc stream ưu tiên trước; chưa có việc nào thì chờ tối
    đa `wait_ms` trên các stream còn slot, có việc thì lấy thêm cho đủ slot. Việc đã quá
    `deadline_ms` bị bỏ (không giao). Việc giao được ghi vào `Keys.inflight(consumer)`.
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
        # Chờ việc đầu tiên trên mọi stream còn slot (BLOCK 0 là chờ mãi: luôn ≥ 1 ms). COUNT 1:
        # Redis trả ngay sau lệnh XADD đầu tiên nên thường chỉ một stream có việc; phần còn lại lấy
        # bằng `top_up` theo đúng thứ tự ưu tiên.
        if await lease.read(dict.fromkeys(streams, 1), block_ms=max(1, left_ms)):
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


async def release_worker(redis: Redis, keys: Keys, worker_id: str) -> int:
    """Worker rời / quá hạn báo sống: trả `ERROR_KIND_WORKER_LOST` cho mọi việc nó còn giữ (backend
    thử lại ngay) rồi xoá danh sách đó. HGETALL + DEL trong một transaction: Complete chạy song song
    hoặc nhận kết quả trước (việc không còn trong danh sách), hoặc bị bỏ. Việc đã quá hạn (backend
    thôi chờ) thì không trả. Trả số việc đã trả về backend."""
    key = keys.inflight(worker_id)
    async with redis.pipeline(transaction=True) as pipe:
        pipe.hgetall(key)
        pipe.delete(key)
        held, _ = await pipe.execute()
    now = _now_ms()
    lost = [
        crawl_pb2.FetchResult(
            task_id=_text(task_id),
            error_kind=crawl_pb2.ERROR_KIND_WORKER_LOST,
            error=WORKER_LOST_ERROR,
            worker_id=worker_id,
        )
        for task_id, deadline in (held or {}).items()
        if not int(deadline) or int(deadline) > now
    ]
    if not lost:
        return 0
    accepted, _ = await push_results(redis, keys, lost)
    return accepted


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
        # Worker bỏ việc quá mốc này: đúng lúc backend thôi chờ hẳn.
        task.deadline_ms = _now_ms() + int((timeout + self.max_wait) * 1000)
        future: asyncio.Future[crawl_pb2.FetchResult] = asyncio.get_running_loop().create_future()
        self._pending[task.task_id] = future
        entry_id: Any = None
        answered = False
        try:
            try:
                async with self.redis.pipeline(transaction=False) as pipe:
                    pipe.get(progress)
                    pipe.xadd(
                        stream,
                        {"t": task.SerializeToString()},
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
