"""Gateway gRPC `CrawlGateway` cho worker lấy trang (`jobcrawler gateway`).

Không giữ trạng thái trong process: worker nằm trong registry (Redis), việc trong Redis Streams
(`dispatch`), việc worker đang giữ trong hash `inflight:<worker>`, kết quả LPUSH về list của process
backend ghi trong `task_id`. Nhiều replica (cổng / máy khác nhau, cùng Redis) chạy song song được;
worker gọi replica nào cũng được.

Complete chỉ nhận kết quả của việc worker còn giữ. Worker rời (Leave) hoặc quá hạn báo sống (prune)
thì việc nó còn giữ được trả về backend ngay với `ERROR_KIND_WORKER_LOST` để backend thử lại.
Heartbeat kèm danh sách việc worker còn nợ kết quả (`task_ids`): việc đã giao mà worker không kể
(lời trả Lease bị mất) cũng được trả `worker_lost`; việc worker kể mà gateway đã thu hồi thì báo lại
(`revoked_task_ids`) để worker huỷ, không tải tiếp vô ích.

Cấu hình đẩy xuống worker lấy từ `settings_provider` mỗi CONFIG_CACHE_SECONDS (đổi ở trang Cài
đặt thì worker nhận ở lần Lease / Heartbeat sau qua `config_version`).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import re
import sys
import time
from collections.abc import Awaitable, Callable
from typing import Any

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc
from redis.asyncio import Redis
from redis.exceptions import RedisError

from jcproto.v1 import crawl_pb2, crawl_pb2_grpc
from jobcrawler.config import Settings
from jobcrawler.gateway.dispatch import (
    complete_results,
    lease_tasks,
    reconcile_worker,
    release_expired_worker,
    release_worker,
)
from jobcrawler.gateway.registry import (
    DEFAULT_POOL,
    Keys,
    WorkerStats,
    WorkerStatus,
    add_counts,
    expired_workers,
    remove_worker,
    save_worker,
    touch_worker,
)
from jobcrawler.gateway.worker_auth import WorkerAuth, bearer
from jobcrawler.logging import get_logger
from jobcrawler.net.proxy_pool import central_proxies

log = get_logger(__name__)

SERVICE_NAME = "jobcrawler.crawl.v1.CrawlGateway"
MAX_MESSAGE_BYTES = 64 * 1024 * 1024
MAX_WAIT_MS = 30_000
PRUNE_SECONDS = 10.0
CONFIG_CACHE_SECONDS = 5.0
STOP_GRACE_SECONDS = 10.0
_WORKER_ID = re.compile(r"^[A-Za-z0-9_.:@-]{1,128}$", re.ASCII)
_POOL = re.compile(r"^[A-Za-z0-9_.-]{1,64}$", re.ASCII)
_INT32_MAX = 2**31 - 1

# (JSON `WorkerStatus` nếu worker còn trong hạn báo sống theo đồng hồ Redis, đang drain 0/1).
# KEYS: workers, worker:<id>, drain. ARGV: worker_id.
_LOOKUP = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local draining = redis.call('SISMEMBER', KEYS[3], ARGV[1])
local score = redis.call('ZSCORE', KEYS[1], ARGV[1])
if not score or tonumber(score) <= now then
  return {'', draining}
end
return {redis.call('GET', KEYS[2]) or '', draining}
"""

SettingsProvider = Callable[[], Settings]


def _now_ms() -> int:
    return int(time.time() * 1000)


# --- cấu hình worker -----------------------------------------------------------------------------


def config_version(fields: dict[str, Any]) -> int:
    """Phiên bản ổn định (int64 dương, khác 0) của bộ cấu hình: cùng nội dung → cùng số."""
    digest = hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).digest()
    return (int.from_bytes(digest[:8], "big") >> 1) or 1


def shared_proxies(settings: Settings) -> list[str]:
    """Proxy chung gửi xuống worker (PROXY_MODE=worker): worker không có proxy riêng thì tự xoay
    vòng trong danh sách này. Chế độ khác: rỗng (central: backend chọn từng request)."""
    if settings.proxy_mode != "worker":
        return []
    return [p.url for p in central_proxies(settings.proxies, settings.proxies_file)]


def proxies_hash(urls: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(urls)).encode()).hexdigest()[:16] if urls else ""


def worker_config(settings: Settings) -> crawl_pb2.WorkerConfig:
    shared = shared_proxies(settings)
    fields: dict[str, Any] = {
        "http_slots": settings.gateway_http_slots,
        "render_slots": settings.gateway_render_slots,
        "impersonate": settings.gateway_impersonate,
        "max_body_bytes": min(settings.fetch_max_body_bytes, _INT32_MAX),
        "heartbeat_seconds": settings.gateway_heartbeat_seconds,
        "lease_wait_ms": settings.gateway_lease_wait_ms,
        "use_local_proxies": settings.proxy_mode == "worker",
        # Mã băm (không phải danh sách, có mật khẩu) nằm trong phiên bản: đổi danh sách → worker
        # thấy phiên bản mới ở Lease / Heartbeat → đăng ký lại để nhận danh sách.
        "shared_proxies_hash": proxies_hash(shared),
    }
    return crawl_pb2.WorkerConfig(version=config_version(fields), shared_proxies=shared, **fields)


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
        auth: WorkerAuth | None = None,
    ) -> None:
        self.redis = redis
        self.keys = keys
        # Xác thực worker (`gateway/worker_auth.py`); None / tắt = không hỏi (GATEWAY_TOKEN trống).
        self.auth = auth
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
        found: Any = await self.redis.eval(  # pyright: ignore[reportGeneralTypeIssues]
            _LOOKUP,
            3,
            self.keys.workers,
            self.keys.worker(worker_id),
            self.keys.drain,
            worker_id,
        )
        raw, draining = found
        return (raw or None), bool(draining)

    async def _authorize(self, context: grpc.aio.ServicerContext, worker_id: str) -> str | None:
        """Lease / Complete / Heartbeat / Leave: token riêng của đúng worker này. Trả token (để
        gia hạn), None khi gateway không bật xác thực. Sai thì huỷ lời gọi."""
        if self.auth is None or not self.auth.enabled:
            return None
        token = bearer(context.invocation_metadata())
        record = await self.auth.check_token(token) if token else None
        if token is None or record is None:
            log.warning("gateway.unauthenticated", worker_id=worker_id)
            await context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "token worker không hợp lệ hoặc đã hết hạn: Register lại bằng khoá tham gia",
            )
            raise AssertionError("unreachable")  # context.abort luôn ném
        if record.worker_id != worker_id:
            await context.abort(
                grpc.StatusCode.PERMISSION_DENIED, "token thuộc worker khác: Register lại"
            )
        return token

    async def _join(
        self, context: grpc.aio.ServicerContext, worker_id: str
    ) -> tuple[str, str | None]:
        """Register: (cách vào, token mới cho worker hoặc None = giữ token đang dùng)."""
        assert self.auth is not None
        credential = bearer(context.invocation_metadata())
        if credential is None:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "thiếu khoá: gửi Bearer <khoá>")
            raise AssertionError("unreachable")
        if self.auth.is_static(credential):
            return "static", await self.auth.issue_token(worker_id=worker_id, via="static")
        record = await self.auth.check_token(credential)
        if record is not None:
            await self.auth.bind(credential, record, worker_id)
            return "token", None
        join_id = await self.auth.consume_join_key(credential, worker_id=worker_id)
        if join_id is not None:
            log.info("gateway.joined", worker_id=worker_id, join_id=join_id)
            return "join", await self.auth.issue_token(worker_id=worker_id, via="join")
        log.warning("gateway.unauthenticated", worker_id=worker_id, method="Register")
        await context.abort(
            grpc.StatusCode.UNAUTHENTICATED,
            "khoá không hợp lệ, hết hạn hoặc khoá tham gia đã được dùng",
        )
        raise AssertionError("unreachable")

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
        via, worker_token = (
            await self._join(context, info.worker_id)
            if self.auth is not None and self.auth.enabled
            else ("open", None)
        )
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
            auth=via,
        )
        return crawl_pb2.RegisterResponse(config=config, worker_token=worker_token or "")

    @_redis_guard
    async def Lease(
        self, request: crawl_pb2.LeaseRequest, context: grpc.aio.ServicerContext
    ) -> crawl_pb2.LeaseResponse:
        worker_id = request.worker_id
        await self._authorize(context, worker_id)
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
        await self._authorize(context, worker_id)
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
        token = await self._authorize(context, worker_id)
        if token is not None and self.auth is not None:
            await self.auth.touch(token)  # worker còn sống: token không hết hạn
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
        revoked: list[str] = []
        if request.reports_tasks:  # worker cũ không kể việc: không đối soát
            lost, revoked = await reconcile_worker(
                self.redis, self.keys, worker_id, request.task_ids
            )
            if lost:
                ttl = settings.gateway_worker_ttl_seconds
                await add_counts(self.redis, self.keys, worker_id, ttl=ttl, lost=lost)
                log.warning("gateway.worker_lost", worker_id=worker_id, tasks=lost, reason="orphan")
            if revoked:
                log.info("gateway.revoked", worker_id=worker_id, tasks=len(revoked))
        return crawl_pb2.HeartbeatResponse(
            config_version=config.version, drain=draining, revoked_task_ids=revoked
        )

    @_redis_guard
    async def Leave(
        self, request: crawl_pb2.LeaveRequest, context: grpc.aio.ServicerContext
    ) -> crawl_pb2.LeaveResponse:
        worker_id = request.worker_id
        await self._authorize(context, worker_id)
        await remove_worker(self.redis, self.keys, worker_id)
        lost = await self._release(worker_id, reason="leave")
        log.info("gateway.leave", worker_id=worker_id, lost=lost)
        return crawl_pb2.LeaveResponse()

    async def _count_lost(self, worker_id: str, lost: int, *, reason: str) -> None:
        if lost:
            ttl = self.settings().gateway_worker_ttl_seconds
            await add_counts(self.redis, self.keys, worker_id, ttl=ttl, lost=lost)
            log.warning("gateway.worker_lost", worker_id=worker_id, tasks=lost, reason=reason)

    async def _release(self, worker_id: str, *, reason: str) -> int:
        """Trả `worker_lost` cho việc worker còn giữ (backend thử lại ngay); đếm vào `lost`."""
        lost = await release_worker(self.redis, self.keys, worker_id)
        await self._count_lost(worker_id, lost, reason=reason)
        return lost

    async def prune(self) -> list[str]:
        """Bỏ worker quá hạn báo sống và trả `worker_lost` cho việc nó còn giữ; worker vừa
        Register lại giữa lúc đó thì để nguyên. Trả id đã bỏ."""
        pruned: list[str] = []
        for worker_id in await expired_workers(self.redis, self.keys):
            lost = await release_expired_worker(self.redis, self.keys, worker_id)
            if lost is None:  # vừa báo sống / đăng ký lại
                continue
            await self._count_lost(worker_id, lost, reason="expired")
            pruned.append(worker_id)
        if pruned:
            log.info("gateway.prune", workers=pruned)
        return pruned


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
        keys = Keys(settings.redis_prefix)
        token = settings.gateway_token.get_secret_value() if settings.gateway_token else None
        self.servicer = CrawlGateway(
            redis,
            keys,
            settings_provider or (lambda: settings),
            config_cache_seconds=config_cache_seconds,
            auth=WorkerAuth(redis, keys.base, static_token=token),
        )
        self.health = health.aio.HealthServicer()  # pyright: ignore[reportAttributeAccessIssue]
        self.port = 0
        self._prune_seconds = prune_seconds
        self._server: grpc.aio.Server | None = None
        self._pruner: asyncio.Task[None] | None = None

    async def start(self, host: str | None = None, port: int | None = None) -> int:
        """Mở cổng (`port=0`: cổng ngẫu nhiên) và chạy; trả cổng đã mở."""
        s = self.settings
        # Xác thực worker nằm trong từng RPC (`CrawlGateway._join` / `_authorize`): cần biết
        # worker_id của request để gắn token với đúng worker. Health check không cần khoá.
        server = grpc.aio.server(options=server_options())
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
        auth = self.servicer.auth is not None and self.servicer.auth.enabled
        log.info("gateway.start", address=address, port=self.port, tls=tls, auth=auth)
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
