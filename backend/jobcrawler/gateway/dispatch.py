"""Hàng việc lấy trang trên Redis Streams: backend gửi việc và chờ kết quả, gateway giao việc.

- Mỗi pool hai stream (`Keys.stream(pool, "http" | "render")`), entry `{t: FetchTask protobuf}`,
  consumer group `workers`, mỗi worker là một consumer. Gateway đọc hộ worker bằng XREADGROUP
  NOACK rồi XDEL ngay: mỗi việc giao tối đa một lần; mất việc (gateway chết giữa chừng) thì phía
  backend hết hạn chờ và `HttpClient` thử lại.
- `task_id` = `<client_id>.<seq>`: gateway đọc `client_id` từ đó rồi LPUSH `FetchResult` vào
  `Keys.reply(client_id)` (hết hạn sau REPLY_TTL_SECONDS), nên gateway không giữ trạng thái.
- `FetchClient` (mỗi process backend một cái): XADD việc, một task nền BRPOP list kết quả của
  mình trên kết nối Redis riêng và trả kết quả cho việc đang chờ theo `task_id`.
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
from collections.abc import Iterable, Mapping
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
_TASK_ID = re.compile(r"^([A-Za-z0-9_-]{1,128})\.(\d{1,20})$", re.ASCII)


class DispatchError(TransportError):
    """Lỗi của hàng việc (không có worker, worker không trả kết quả), không do proxy hay trang."""


class NoWorkerError(DispatchError):
    """Không có worker nào sống cho pool / loại việc: báo lỗi ngay, không xếp hàng chờ."""

    def __init__(self, message: str) -> None:
        super().__init__("other", f"no_worker: {message}")


class NoReplyError(DispatchError):
    """Quá `timeout + FETCH_QUEUE_GRACE_SECONDS` mà không worker nào trả kết quả."""

    def __init__(self, message: str) -> None:
        super().__init__("timeout", message)


def _now_ms() -> int:
    return int(time.time() * 1000)


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


async def delete_consumer(redis: Redis, keys: Keys, pool: str, consumer: str) -> None:
    """Bỏ consumer của worker đã rời (NOACK nên không còn entry nào đang giữ)."""
    for kind in KINDS:
        with contextlib.suppress(ResponseError):  # NOGROUP: stream đã bị xoá
            await redis.xgroup_delconsumer(keys.stream(pool, kind), GROUP, consumer)


def _entries(resp: Any) -> list[tuple[str, list[tuple[Any, Any]]]]:
    """Kết quả XREADGROUP (RESP2: list, RESP3: dict) → [(stream, [(id, fields)])]."""
    if not resp:
        return []
    out: list[tuple[str, list[tuple[Any, Any]]]] = []
    items = resp.items() if isinstance(resp, dict) else resp
    for stream, entries in items:
        if entries and isinstance(entries[0], list):  # RESP3: [[(id, fields), ...]]
            entries = entries[0]
        name = stream.decode() if isinstance(stream, bytes) else str(stream)
        out.append((name, list(entries or [])))
    return out


async def _read(
    redis: Redis, streams: Mapping[str, int], consumer: str, *, block_ms: int | None
) -> tuple[list[crawl_pb2.FetchTask], dict[str, int]]:
    """Một lần XREADGROUP NOACK (`streams`: stream → số việc tối đa; nhiều stream thì COUNT là số
    nhỏ nhất) rồi XDEL. Trả (việc còn hạn, số entry đã đọc theo stream)."""
    count = min(streams.values())
    try:
        resp = await redis.xreadgroup(
            GROUP, consumer, dict.fromkeys(streams, ">"), count=count, block=block_ms, noack=True
        )
    except ResponseError as exc:
        if "NOGROUP" not in str(exc):
            raise
        for stream in streams:
            await ensure_group(redis, stream)
        return [], {}
    tasks: list[crawl_pb2.FetchTask] = []
    taken: dict[str, int] = {}
    now = _now_ms()
    expired = 0
    for stream, entries in _entries(resp):
        if not entries:
            continue
        taken[stream] = len(entries)
        await redis.xdel(stream, *[entry_id for entry_id, _ in entries])
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
            tasks.append(task)
    if expired:
        log.info("gateway.expired", consumer=consumer, tasks=expired)
    return tasks, taken


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

    Đọc ngay (không chờ) trước; chưa có việc nào thì chờ tối đa `wait_ms` trên các stream còn
    slot, có việc thì lấy thêm cho đủ slot. Việc đã quá `deadline_ms` bị bỏ (không giao).
    """
    slots = {
        keys.stream(pool, kind): n for kind, n in (("render", render), ("http", http)) if n > 0
    }
    if not slots:
        return []
    deadline = time.monotonic() + max(0, wait_ms) / 1000
    tasks: list[crawl_pb2.FetchTask] = []

    async def top_up() -> None:
        for stream, n in slots.items():
            if n > 0:
                got, taken = await _read(redis, {stream: n}, consumer, block_ms=None)
                tasks.extend(got)
                slots[stream] = n - taken.get(stream, 0)

    await top_up()
    while not tasks:
        left_ms = int((deadline - time.monotonic()) * 1000)
        open_slots = {s: n for s, n in slots.items() if n > 0}
        if left_ms < 1 or not open_slots:
            break
        # BLOCK 0 là chờ mãi: luôn ≥ 1 ms.
        got, taken = await _read(redis, open_slots, consumer, block_ms=max(1, left_ms))
        tasks.extend(got)
        for stream, n in taken.items():
            slots[stream] -= n
        if taken:
            await top_up()
    return tasks


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
    `task_id` + `deadline_ms`, XADD, rồi chờ tối đa `timeout_seconds + FETCH_QUEUE_GRACE_SECONDS`.
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
        self.maxlen = settings.fetch_stream_maxlen
        self.max_body_bytes = settings.fetch_max_body_bytes
        self._blocking = _clone(redis)
        self._pending: dict[str, asyncio.Future[crawl_pb2.FetchResult]] = {}
        self._seq = itertools.count(1)
        self._reader: asyncio.Task[None] | None = None
        self._closed = False
        self._live: dict[str, dict[str, int]] = {}
        self._live_at = -math.inf
        self._live_lock = asyncio.Lock()
        self.counters: Counter[str] = Counter(
            submitted=0, completed=0, timeouts=0, no_worker=0, discarded=0
        )

    def stats(self) -> dict[str, int]:
        """Số việc đã gửi / có kết quả / hết hạn chờ / không có worker / kết quả bị bỏ (muộn,
        không rõ việc) trong process này, và số việc đang chờ."""
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
        """Gửi `task` (gán `task_id`, `deadline_ms`) và chờ kết quả.

        Lỗi: `NoWorkerError` (không có worker sống), `NoReplyError` (hết hạn chờ),
        `DispatchError` (Redis lỗi, client đã đóng)."""
        if self._closed:
            raise DispatchError("other", "FetchClient đã đóng")
        self._start()
        kind = task_kind(task)
        pool = await self.pool_for(task.url, kind)
        wait = max(0.0, task.timeout_seconds) + self.grace
        task.task_id = f"{self.client_id}.{next(self._seq)}"
        task.deadline_ms = _now_ms() + int(wait * 1000)
        future: asyncio.Future[crawl_pb2.FetchResult] = asyncio.get_running_loop().create_future()
        self._pending[task.task_id] = future
        try:
            try:
                await self.redis.xadd(
                    self.keys.stream(pool, kind),
                    {"t": task.SerializeToString()},
                    maxlen=self.maxlen,
                    approximate=True,
                )
            except RedisError as exc:
                raise DispatchError("other", f"redis: {exc}") from exc
            self.counters["submitted"] += 1
            try:
                async with asyncio.timeout(wait):
                    result = await future
            except TimeoutError:
                self.counters["timeouts"] += 1
                log.debug("fetch.no_reply", url=task.url, pool=pool, kind=kind, wait=wait)
                raise NoReplyError(
                    f"không có worker trả kết quả trong {wait:.0f} s (pool {pool}, {kind})"
                ) from None
            self.counters["completed"] += 1
            return result
        finally:
            self._pending.pop(task.task_id, None)

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
        await self._blocking.aclose()
