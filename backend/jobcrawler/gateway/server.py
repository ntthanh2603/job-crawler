"""Gateway gRPC `CrawlGateway` cho worker lấy trang (`jobcrawler gateway`).

Không giữ trạng thái trong process: worker nằm trong registry (Redis), việc trong Redis Streams
(`dispatch`), việc worker đang giữ trong hash `inflight:<worker>`, kết quả LPUSH về list của process
backend ghi trong `task_id`. Nhiều replica (cổng / máy khác nhau, cùng Redis) chạy song song được;
worker gọi replica nào cũng được.

Complete chỉ nhận kết quả của việc worker còn giữ. Worker rời (Leave) hoặc quá hạn báo sống (prune)
thì việc nó còn giữ được trả về backend ngay với `ERROR_KIND_WORKER_LOST` để backend thử lại.

Cấu hình đẩy xuống worker lấy từ `settings_provider` mỗi CONFIG_CACHE_SECONDS (đổi ở trang Cài
đặt thì worker nhận ở lần Lease / Heartbeat sau qua `config_version`).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import hmac
import json
import re
import sys
import time
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from redis.asyncio import Redis
from redis.exceptions import RedisError

from jcproto.v1 import crawl_pb2, crawl_pb2_grpc
from jobcrawler.config import Settings
from jobcrawler.gateway.dispatch import (
    complete_results,
    delete_consumer,
    ensure_groups,
    lease_tasks,
    release_worker,
)
from jobcrawler.gateway.registry import (
    DEFAULT_POOL,
    Keys,
    WorkerStats,
    WorkerStatus,
    add_counts,
    expired_workers,
    prune,
    remove_worker,
    save_worker,
    touch_worker,
)
from jobcrawler.logging import get_logger

log = get_logger(__name__)

SERVICE_NAME = "jobcrawler.crawl.v1.CrawlGateway"
MAX_MESSAGE_BYTES = 64 * 1024 * 1024
MAX_WAIT_MS = 30_000
PRUNE_SECONDS = 10.0
CONFIG_CACHE_SECONDS = 5.0
STOP_GRACE_SECONDS = 10.0
_PUBLIC_PREFIX = "/grpc.health.v1.Health/"  # health check không cần token
_WORKER_ID = re.compile(r"^[A-Za-z0-9_.:@-]{1,128}$", re.ASCII)
_POOL = re.compile(r"^[A-Za-z0-9_.-]{1,64}$", re.ASCII)
_INT32_MAX = 2**31 - 1

SettingsProvider = Callable[[], Settings]


def _now_ms() -> int:
    return int(time.time() * 1000)


# --- cấu hình worker -----------------------------------------------------------------------------


def config_version(fields: dict[str, Any]) -> int:
    """Phiên bản ổn định (int64 dương, khác 0) của bộ cấu hình: cùng nội dung → cùng số."""
    digest = hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).digest()
    return (int.from_bytes(digest[:8], "big") >> 1) or 1


def worker_config(settings: Settings) -> crawl_pb2.WorkerConfig:
    fields: dict[str, Any] = {
        "http_slots": settings.gateway_http_slots,
        "render_slots": settings.gateway_render_slots,
        "impersonate": settings.gateway_impersonate,
        "max_body_bytes": min(settings.fetch_max_body_bytes, _INT32_MAX),
        "heartbeat_seconds": settings.gateway_heartbeat_seconds,
        "lease_wait_ms": settings.gateway_lease_wait_ms,
        "use_local_proxies": settings.proxy_mode == "worker",
    }
    return crawl_pb2.WorkerConfig(version=config_version(fields), **fields)


# --- xác thực ------------------------------------------------------------------------------------


def bearer_ok(metadata: Iterable[tuple[str, str | bytes]] | None, token: str) -> bool:
    """Metadata có `authorization: Bearer <token>` (so sánh thời gian hằng)."""
    expected = f"Bearer {token}".encode()
    for key, value in metadata or ():
        if key.lower() == "authorization":
            got = value if isinstance(value, bytes) else value.encode()
            return hmac.compare_digest(got, expected)
    return False


def _denied(handler: grpc.RpcMethodHandler) -> grpc.RpcMethodHandler:
    async def abort(_request: Any, context: grpc.aio.ServicerContext) -> None:
        await context.abort(
            grpc.StatusCode.UNAUTHENTICATED,
            "thiếu hoặc sai token (metadata authorization: Bearer <GATEWAY_TOKEN>)",
        )

    codec = {
        "request_deserializer": handler.request_deserializer,
        "response_serializer": handler.response_serializer,
    }
    if handler.unary_unary:
        return grpc.unary_unary_rpc_method_handler(abort, **codec)
    if handler.unary_stream:
        return grpc.unary_stream_rpc_method_handler(abort, **codec)
    if handler.stream_unary:
        return grpc.stream_unary_rpc_method_handler(abort, **codec)
    return grpc.stream_stream_rpc_method_handler(abort, **codec)


class TokenInterceptor(grpc.aio.ServerInterceptor):
    """Từ chối (UNAUTHENTICATED) mọi lời gọi thiếu token, trừ health check."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], Awaitable[grpc.RpcMethodHandler]],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler:
        handler = await continuation(handler_call_details)
        method: str = handler_call_details.method  # pyright: ignore[reportAttributeAccessIssue]
        metadata = handler_call_details.invocation_metadata  # pyright: ignore[reportAttributeAccessIssue]
        if handler is None or method.startswith(_PUBLIC_PREFIX) or bearer_ok(metadata, self._token):
            return handler
        log.warning("gateway.unauthenticated", method=method)
        return _denied(handler)


# --- servicer ------------------------------------------------------------------------------------

Handler = Callable[[Any, Any, grpc.aio.ServicerContext], Awaitable[Any]]


def _redis_guard(fn: Handler) -> Handler:
    """Redis lỗi → UNAVAILABLE (worker chờ rồi gọi lại) thay vì UNKNOWN kèm traceback."""

    @functools.wraps(fn)
    async def wrapper(self: Any, request: Any, context: grpc.aio.ServicerContext) -> Any:
        try:
            return await fn(self, request, context)
        except RedisError as exc:
            log.warning("gateway.redis_error", method=fn.__name__, error=repr(exc))
            await context.abort(grpc.StatusCode.UNAVAILABLE, f"redis: {exc}")

    return wrapper


class CrawlGateway:
    """Cài đặt các RPC của `CrawlGateway` (crawl.proto) trên Redis."""

    def __init__(
        self,
        redis: Redis,
        keys: Keys,
        settings_provider: SettingsProvider,
        *,
        config_cache_seconds: float = CONFIG_CACHE_SECONDS,
    ) -> None:
        self.redis = redis
        self.keys = keys
        self._provider = settings_provider
        self._cache_seconds = config_cache_seconds
        self._cached: tuple[float, Settings, crawl_pb2.WorkerConfig] | None = None

    def _current(self) -> tuple[Settings, crawl_pb2.WorkerConfig]:
        now = time.monotonic()
        if self._cached is None or now - self._cached[0] >= self._cache_seconds:
            settings = self._provider()
            self._cached = (now, settings, worker_config(settings))
        return self._cached[1], self._cached[2]

    def settings(self) -> Settings:
        return self._current()[0]

    def config(self) -> crawl_pb2.WorkerConfig:
        return self._current()[1]

    async def _lookup(self, worker_id: str) -> tuple[bytes | None, bool]:
        """(JSON `WorkerStatus` nếu worker còn trong hạn báo sống, đang drain)."""
        async with self.redis.pipeline(transaction=False) as pipe:
            pipe.zscore(self.keys.workers, worker_id)
            pipe.get(self.keys.worker(worker_id))
            pipe.sismember(self.keys.drain, worker_id)
            score, raw, draining = await pipe.execute()
        if score is None or score <= _now_ms() or raw is None:
            return None, bool(draining)
        return raw, bool(draining)

    @staticmethod
    async def _unknown(context: grpc.aio.ServicerContext, worker_id: str) -> None:
        await context.abort(
            grpc.StatusCode.NOT_FOUND,
            f"worker {worker_id!r} chưa đăng ký hoặc đã hết hạn báo sống: gọi Register",
        )

    @_redis_guard
    async def Register(
        self, request: crawl_pb2.RegisterRequest, context: grpc.aio.ServicerContext
    ) -> crawl_pb2.RegisterResponse:
        info = request.info
        pool = info.pool or DEFAULT_POOL
        if not _WORKER_ID.match(info.worker_id):
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "worker_id không hợp lệ")
        if not _POOL.match(pool):
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, f"pool không hợp lệ: {pool!r}")
        settings, config = self._current()
        status = WorkerStatus(
            worker_id=info.worker_id,
            hostname=info.hostname,
            version=info.version,
            pool=pool,
            http_slots=info.http_slots,
            render_slots=info.render_slots,
            local_proxies=info.local_proxies,
            labels=dict(info.labels),
            started_at_ms=info.started_at_ms,
            registered_at_ms=_now_ms(),
        )
        await ensure_groups(self.redis, self.keys, pool)
        await save_worker(self.redis, self.keys, status, ttl=settings.gateway_worker_ttl_seconds)
        log.info(
            "gateway.register",
            worker_id=info.worker_id,
            pool=pool,
            hostname=info.hostname,
            version=info.version,
            http_slots=info.http_slots,
            render_slots=info.render_slots,
            local_proxies=info.local_proxies,
        )
        return crawl_pb2.RegisterResponse(config=config)

    @_redis_guard
    async def Lease(
        self, request: crawl_pb2.LeaseRequest, context: grpc.aio.ServicerContext
    ) -> crawl_pb2.LeaseResponse:
        worker_id = request.worker_id
        raw, draining = await self._lookup(worker_id)
        if raw is None:
            await self._unknown(context, worker_id)
            return crawl_pb2.LeaseResponse()
        settings, config = self._current()
        await touch_worker(
            self.redis, self.keys, worker_id, ttl=settings.gateway_worker_ttl_seconds
        )
        if draining:
            return crawl_pb2.LeaseResponse(config_version=config.version, drain=True)
        wait_ms = min(max(request.wait_ms, 0), MAX_WAIT_MS)
        remaining = context.time_remaining()
        if remaining is not None:  # trả lời trước deadline của lời gọi
            wait_ms = min(wait_ms, max(0, int(remaining * 1000) - 250))
        http, render = max(0, request.http_slots), max(0, request.render_slots)
        if not http and not render:  # hết slot: vẫn chờ như long-poll để worker không gọi dồn
            await asyncio.sleep(wait_ms / 1000)
            return crawl_pb2.LeaseResponse(config_version=config.version)
        pool = json.loads(raw).get("pool") or DEFAULT_POOL
        tasks = await lease_tasks(
            self.redis, self.keys, pool, worker_id, http=http, render=render, wait_ms=wait_ms
        )
        if tasks:
            await add_counts(
                self.redis,
                self.keys,
                worker_id,
                ttl=settings.gateway_worker_ttl_seconds,
                leased=len(tasks),
            )
            log.debug("gateway.lease", worker_id=worker_id, pool=pool, tasks=len(tasks))
        return crawl_pb2.LeaseResponse(tasks=tasks, config_version=config.version)

    @_redis_guard
    async def Complete(
        self, request: crawl_pb2.CompleteRequest, context: grpc.aio.ServicerContext
    ) -> crawl_pb2.CompleteResponse:
        worker_id = request.worker_id
        accepted, malformed, stale = await complete_results(
            self.redis, self.keys, worker_id, request.results
        )
        if (accepted or stale) and worker_id:
            ttl = self.settings().gateway_worker_ttl_seconds
            await add_counts(
                self.redis, self.keys, worker_id, ttl=ttl, completed=accepted, dropped=stale
            )
        if malformed:
            log.warning("gateway.bad_results", worker_id=worker_id, dropped=malformed)
        if stale:
            # Việc đã trả `worker_lost` cho backend (worker từng quá hạn báo sống) hoặc không phải
            # của worker này: backend đã / sẽ thử lại, kết quả này bỏ.
            log.debug("gateway.stale_results", worker_id=worker_id, dropped=stale)
        log.debug("gateway.complete", worker_id=worker_id, accepted=accepted)
        return crawl_pb2.CompleteResponse(accepted=accepted, dropped=malformed + stale)

    @_redis_guard
    async def Heartbeat(
        self, request: crawl_pb2.HeartbeatRequest, context: grpc.aio.ServicerContext
    ) -> crawl_pb2.HeartbeatResponse:
        worker_id = request.worker_id
        raw, draining = await self._lookup(worker_id)
        if raw is None:
            await self._unknown(context, worker_id)
            return crawl_pb2.HeartbeatResponse()
        settings, config = self._current()
        status = WorkerStatus.model_validate_json(raw)
        stats = request.stats
        status.stats = WorkerStats(
            **{name: getattr(stats, name) for name in WorkerStats.model_fields}
        )
        await save_worker(self.redis, self.keys, status, ttl=settings.gateway_worker_ttl_seconds)
        return crawl_pb2.HeartbeatResponse(config_version=config.version, drain=draining)

    @_redis_guard
    async def Leave(
        self, request: crawl_pb2.LeaveRequest, context: grpc.aio.ServicerContext
    ) -> crawl_pb2.LeaveResponse:
        worker_id = request.worker_id
        raw = await self.redis.get(self.keys.worker(worker_id))
        await remove_worker(self.redis, self.keys, worker_id)
        lost = await self._release(worker_id, reason="leave")
        if raw is not None:
            pool = json.loads(raw).get("pool") or DEFAULT_POOL
            await delete_consumer(self.redis, self.keys, pool, worker_id)
        log.info("gateway.leave", worker_id=worker_id, lost=lost)
        return crawl_pb2.LeaveResponse()

    async def _release(self, worker_id: str, *, reason: str) -> int:
        """Trả `worker_lost` cho việc worker còn giữ (backend thử lại ngay); đếm vào `lost`."""
        lost = await release_worker(self.redis, self.keys, worker_id)
        if lost:
            ttl = self.settings().gateway_worker_ttl_seconds
            await add_counts(self.redis, self.keys, worker_id, ttl=ttl, lost=lost)
            log.warning("gateway.worker_lost", worker_id=worker_id, tasks=lost, reason=reason)
        return lost

    async def prune(self) -> list[str]:
        """Bỏ worker quá hạn báo sống (consumer của nó trong stream, việc nó còn giữ). Trả id đã
        bỏ."""
        expired = await expired_workers(self.redis, self.keys)
        if not expired:
            return []
        raws = await self.redis.mget([self.keys.worker(w) for w in expired])
        for worker_id, raw in zip(expired, raws, strict=True):
            pool = (json.loads(raw).get("pool") if raw else None) or DEFAULT_POOL
            await delete_consumer(self.redis, self.keys, pool, worker_id)
            await self._release(worker_id, reason="expired")
        await prune(self.redis, self.keys)
        log.info("gateway.prune", workers=expired)
        return expired


# --- server --------------------------------------------------------------------------------------


def server_options() -> list[tuple[str, int]]:
    options = [
        ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
        ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
        # Worker giữ kết nối lâu (long-poll), ping keepalive cả khi không có lời gọi.
        ("grpc.keepalive_time_ms", 30_000),
        ("grpc.keepalive_timeout_ms", 10_000),
        ("grpc.keepalive_permit_without_calls", 1),
        ("grpc.http2.min_recv_ping_interval_without_data_ms", 10_000),
        ("grpc.http2.min_ping_interval_without_data_ms", 10_000),
        ("grpc.http2.max_ping_strikes", 0),
    ]
    if sys.platform == "win32":
        options.append(("grpc.so_reuseport", 0))
    return options


def _address(host: str, port: int) -> str:
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{host}:{port}"


class GatewayServer:
    """Server gRPC: `CrawlGateway` + health (`grpc.health.v1`), token và TLS theo cấu hình, dọn
    worker quá hạn định kỳ."""

    def __init__(
        self,
        redis: Redis,
        settings: Settings,
        *,
        settings_provider: SettingsProvider | None = None,
        prune_seconds: float = PRUNE_SECONDS,
        config_cache_seconds: float = CONFIG_CACHE_SECONDS,
    ) -> None:
        self.settings = settings
        self.servicer = CrawlGateway(
            redis,
            Keys(settings.redis_prefix),
            settings_provider or (lambda: settings),
            config_cache_seconds=config_cache_seconds,
        )
        self.health = health.aio.HealthServicer()  # pyright: ignore[reportAttributeAccessIssue]
        self.port = 0
        self._prune_seconds = prune_seconds
        self._server: grpc.aio.Server | None = None
        self._pruner: asyncio.Task[None] | None = None

    async def start(self, host: str | None = None, port: int | None = None) -> int:
        """Mở cổng (`port=0`: cổng ngẫu nhiên) và chạy; trả cổng đã mở."""
        s = self.settings
        token = s.gateway_token.get_secret_value() if s.gateway_token else ""
        server = grpc.aio.server(
            interceptors=[TokenInterceptor(token)] if token else None,
            options=server_options(),
        )
        crawl_pb2_grpc.add_CrawlGatewayServicer_to_server(self.servicer, server)
        health_pb2_grpc.add_HealthServicer_to_server(self.health, server)
        address = _address(host or s.gateway_host, s.gateway_port if port is None else port)
        tls = bool(s.gateway_tls_cert and s.gateway_tls_key)
        if s.gateway_tls_cert and s.gateway_tls_key:
            credentials = grpc.ssl_server_credentials(
                [(s.gateway_tls_key.read_bytes(), s.gateway_tls_cert.read_bytes())]
            )
            self.port = server.add_secure_port(address, credentials)
        else:
            self.port = server.add_insecure_port(address)
        await server.start()
        self._server = server
        for service in ("", SERVICE_NAME):
            await self.health.set(service, health_pb2.HealthCheckResponse.SERVING)
        self._pruner = asyncio.get_running_loop().create_task(self._prune_loop())
        log.info("gateway.start", address=address, port=self.port, tls=tls, auth=bool(token))
        return self.port

    async def _prune_loop(self) -> None:
        while True:
            await asyncio.sleep(self._prune_seconds)
            try:
                await self.servicer.prune()
            except (RedisError, OSError) as exc:
                log.warning("gateway.prune_failed", error=repr(exc))

    async def wait(self) -> None:
        """Chờ tới khi server dừng."""
        if self._server is not None:
            await self._server.wait_for_termination()

    async def stop(self, grace: float = STOP_GRACE_SECONDS) -> None:
        """Báo NOT_SERVING, chờ lời gọi đang chạy tối đa `grace` giây rồi dừng."""
        if self._pruner is not None:
            self._pruner.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pruner
            self._pruner = None
        if self._server is None:
            return
        await self.health.enter_graceful_shutdown()
        await self._server.stop(grace)
        self._server = None
        log.info("gateway.stop", port=self.port)
