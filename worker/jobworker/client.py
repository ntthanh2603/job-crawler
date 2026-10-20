"""Vòng lặp gRPC của worker: Register → (Lease → tải → Complete) song song Heartbeat → Leave.

- Lease chỉ xin đúng số slot còn trống (HTTP / render); hết slot thì chờ việc đang chạy xong.
- Kết quả gộp lô (16 kết quả, ~50 ms hoặc ~8 MiB) rồi Complete; gateway mất kết nối thì thử lại vài
  lần rồi bỏ (chấp nhận at-most-once): việc đó thôi được kể trong Heartbeat nên gateway trả
  `worker_lost` cho backend, backend thử lại request.
- Heartbeat kể mọi việc còn nợ kết quả (`task_ids`: đang chạy hoặc chưa Complete xong); gateway
  đối soát với việc đã giao và trả lại `revoked_task_ids` = việc nó đã thu hồi (worker từng quá hạn
  báo sống, đã trả `worker_lost`): worker huỷ việc đó ngay thay vì tải tiếp vô ích.
- `config_version` trong Lease/Heartbeat khác bản đang dùng → Register lại để lấy cấu hình mới.
  NOT_FOUND / FAILED_PRECONDITION (gateway không biết worker này) → Register lại.
- UNAVAILABLE và lỗi kết nối → thử lại với backoff 0.5 s → 30 s, process không bao giờ tự thoát.
  UNAUTHENTICATED → log lỗi, thử lại mỗi phút.
- `drain` (từ gateway) hoặc SIGTERM/Ctrl-C → thôi xin việc, chờ việc đang chạy tối đa
  WORKER_SHUTDOWN_GRACE_SECONDS (kết quả vẫn được Complete), Leave rồi thoát.
- Không mở được Chromium → tạm tắt render (Register lại với render_slots = 0) trong
  min(30 s · 2^(n−1), 10 phút) với n là số lần lỗi liên tiếp, hết hạn thì Register lại với đủ slot
  render và thử lại; mở được thì n về 0. Máy chưa cài Chromium thì tắt hẳn. Trạng thái báo trong
  `WorkerStats.render_disabled_reason` / `render_retry_at_ms`.
- File trạng thái cho healthcheck (WORKER_HEALTH_FILE, `jobworker health`): ghi sau mỗi lời gọi
  gateway thành công / lỗi kết nối và ở mỗi nhịp heartbeat; xem `jobworker.health`.
- Watchdog (WORKER_WATCHDOG_SECONDS): event loop hoặc vòng heartbeat đứng quá hạn → thoát mã 70 để
  Docker chạy lại; mất gateway không tính, lúc dừng chỉ còn hạn tổng; xem `jobworker.watchdog`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import random
import signal
import socket
import sys
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import grpc

from jcproto.v1 import crawl_pb2, crawl_pb2_grpc
from jobworker import __version__
from jobworker.browser import BrowserManager, BrowserUnavailableError
from jobworker.config import WorkerSettings
from jobworker.credentials import Credentials
from jobworker.fetch import HttpFetcher
from jobworker.health import HealthStatus
from jobworker.logging import get_logger
from jobworker.proxies import (
    LocalProxyPool,
    Proxy,
    browser_compatible,
    load_proxies,
    shared_proxies,
    start_offset,
)
from jobworker.results import expired, failure, now_ms, task_timeout
from jobworker.watchdog import Watchdog

log = get_logger(__name__)

MAX_MESSAGE_BYTES = 64 * 1024 * 1024
DEFAULT_HEARTBEAT_SECONDS = 5.0
DEFAULT_LEASE_WAIT_MS = 1000
COMPLETE_BATCH = 16
COMPLETE_FLUSH_SECONDS = 0.05
COMPLETE_MAX_BYTES = 8 * 1024 * 1024
COMPLETE_ATTEMPTS = 4
RPC_TIMEOUT_SECONDS = 15.0
LEAVE_TIMEOUT_SECONDS = 5.0
LEASE_EXTRA_SECONDS = 15.0  # thêm vào wait_ms làm hạn của lời gọi Lease
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_CAP_SECONDS = 30.0
AUTH_RETRY_SECONDS = 60.0
# Chặn cứng: việc chạy quá timeout của nó chừng này giây thì huỷ (phòng curl/Chromium treo).
TASK_GUARD_SECONDS = 30.0
# Không mở được Chromium: tạm tắt render 30 s, 60 s, 120 s... tối đa 10 phút.
RENDER_BACKOFF_BASE_SECONDS = 30.0
RENDER_BACKOFF_CAP_SECONDS = 600.0

REREGISTER_CODES = frozenset({grpc.StatusCode.NOT_FOUND, grpc.StatusCode.FAILED_PRECONDITION})
AUTH_CODES = frozenset({grpc.StatusCode.UNAUTHENTICATED, grpc.StatusCode.PERMISSION_DENIED})
RETRY_CODES = frozenset(
    {
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.DEADLINE_EXCEEDED,
        grpc.StatusCode.RESOURCE_EXHAUSTED,
        grpc.StatusCode.ABORTED,
    }
)


def channel_options() -> list[tuple[str, Any]]:
    return [
        ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
        ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
        # Ping chỉ khi có lời gọi (Lease long-poll gần như luôn mở): server mặc định chặn client
        # ping dày khi không có lời gọi.
        ("grpc.keepalive_time_ms", 30_000),
        ("grpc.keepalive_timeout_ms", 10_000),
        ("grpc.keepalive_permit_without_calls", 0),
        ("grpc.http2.max_pings_without_data", 0),
        ("grpc.initial_reconnect_backoff_ms", 500),
        ("grpc.min_reconnect_backoff_ms", 500),
        ("grpc.max_reconnect_backoff_ms", 30_000),
        ("grpc.primary_user_agent", f"jobworker/{__version__}"),
    ]


def open_channel(settings: WorkerSettings) -> grpc.aio.Channel:
    """Kênh tới gateway: TLS khi GATEWAY_TLS (CA riêng nếu có GATEWAY_CA_CERT)."""
    if settings.gateway_tls:
        root = settings.gateway_ca_cert.read_bytes() if settings.gateway_ca_cert else None
        creds = grpc.ssl_channel_credentials(root_certificates=root)
        return grpc.aio.secure_channel(settings.gateway_addr, creds, options=channel_options())
    return grpc.aio.insecure_channel(settings.gateway_addr, options=channel_options())


async def wait_any(timeout: float, *events: asyncio.Event) -> None:
    """Chờ tới khi một trong các event được set hoặc hết `timeout` giây."""
    waiters = [asyncio.ensure_future(e.wait()) for e in events]
    try:
        await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()


def render_backoff(failures: int) -> float:
    """Giây tạm tắt render sau `failures` lần liên tiếp không mở được Chromium."""
    delay = RENDER_BACKOFF_BASE_SECONDS * 2 ** min(max(0, failures - 1), 30)
    return min(RENDER_BACKOFF_CAP_SECONDS, delay)


def _cap(remote: int, local: int) -> int:
    """Số slot gateway đẩy xuống là trần (> 0), không phải giá trị ghi đè."""
    return min(remote, local) if remote > 0 else local


class Backoff:
    """0.5 s, 1 s, 2 s... tối đa 30 s, nhân ngẫu nhiên 1–1.5 để các worker không dồn cùng lúc."""

    def __init__(
        self, base: float = BACKOFF_BASE_SECONDS, cap: float = BACKOFF_CAP_SECONDS
    ) -> None:
        self.base = base
        self.cap = cap
        self.attempt = 0

    def next(self) -> float:
        delay = min(self.cap, self.base * 2**self.attempt * random.uniform(1.0, 1.5))
        self.attempt = min(self.attempt + 1, 30)
        return delay

    def reset(self) -> None:
        self.attempt = 0


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """Cấu hình đang áp dụng: của worker, kết hợp `WorkerConfig` gateway trả về."""

    http_slots: int
    render_slots: int
    impersonate: str
    max_body_bytes: int
    heartbeat_seconds: float
    lease_wait_ms: int
    use_local_proxies: bool
    version: int = 0
    # Danh sách proxy chung gateway gửi xuống (PROXY_MODE=worker) và mã băm của nó.
    shared_proxies: tuple[str, ...] = ()
    shared_hash: str = ""

    @classmethod
    def local(cls, settings: WorkerSettings) -> RuntimeConfig:
        return cls(
            http_slots=settings.http_slots,
            render_slots=settings.render_slots,
            impersonate=settings.worker_impersonate,
            max_body_bytes=settings.worker_max_body_bytes,
            heartbeat_seconds=DEFAULT_HEARTBEAT_SECONDS,
            lease_wait_ms=DEFAULT_LEASE_WAIT_MS,
            use_local_proxies=True,
        )

    def merge(self, cfg: crawl_pb2.WorkerConfig) -> RuntimeConfig:
        """`self` là cấu hình gốc của worker. Số slot của gateway là trần: min(gateway, worker), để
        máy nhỏ không bị nâng lên bằng máy lớn nhất; WORKER_RENDER_SLOTS=0 thì luôn 0. Các trường
        khác: gateway ghi đè. Trường 0 / rỗng giữ cấu hình của worker."""
        return RuntimeConfig(
            http_slots=_cap(cfg.http_slots, self.http_slots),
            render_slots=_cap(cfg.render_slots, self.render_slots),
            impersonate=cfg.impersonate or self.impersonate,
            max_body_bytes=cfg.max_body_bytes or self.max_body_bytes,
            heartbeat_seconds=float(cfg.heartbeat_seconds or self.heartbeat_seconds),
            lease_wait_ms=cfg.lease_wait_ms or self.lease_wait_ms,
            use_local_proxies=cfg.use_local_proxies,
            version=cfg.version,
            shared_proxies=tuple(cfg.shared_proxies),
            shared_hash=cfg.shared_proxies_hash,
        )


@dataclass(slots=True)
class Counters:
    http_done: int = 0
    http_failed: int = 0
    render_done: int = 0
    render_failed: int = 0


class _CpuMeter:
    """% CPU của process giữa hai lần đo (`os.times`, không tính process Chromium con)."""

    def __init__(self) -> None:
        self._last = (time.monotonic(), self._cpu())

    @staticmethod
    def _cpu() -> float:
        t = os.times()
        return t.user + t.system

    def percent(self) -> float:
        now, cpu = time.monotonic(), self._cpu()
        wall = now - self._last[0]
        used = cpu - self._last[1]
        self._last = (now, cpu)
        return round(100 * used / wall, 1) if wall > 0 else 0.0


def rss_bytes() -> int:
    """RSS của process (Linux: /proc/self/statm); nền tảng khác trả 0."""
    if sys.platform != "linux":
        return 0
    try:
        pages = int(Path("/proc/self/statm").read_text().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return 0


class Worker:
    def __init__(
        self,
        settings: WorkerSettings,
        *,
        channel: grpc.aio.Channel | None = None,
        proxies: LocalProxyPool | None = None,
        http: HttpFetcher | None = None,
        browser: BrowserManager | None = None,
        watchdog: Watchdog | None = None,
    ) -> None:
        self.settings = settings
        self.worker_id = settings.worker_id
        self.local = RuntimeConfig.local(settings)
        self.config = self.local
        if proxies is None:
            proxies = LocalProxyPool(
                load_proxies(settings.worker_proxies, settings.worker_proxies_file),
                sticky_seconds=settings.worker_proxy_sticky_seconds,
                cooldown_seconds=settings.worker_proxy_cooldown_seconds,
                start=start_offset(self.worker_id),
            )
        self.proxies = proxies
        # Máy có proxy riêng thì dùng proxy của nó; không có thì dùng danh sách chung gateway gửi.
        self._own_proxies = len(proxies) > 0
        if http is None:
            http = HttpFetcher(
                worker_id=self.worker_id,
                impersonate=self.config.impersonate,
                max_clients=self.config.http_slots,
                max_body_bytes=self.config.max_body_bytes,
            )
        self.http = http
        self.browser = browser
        if self.browser is None and self.local.render_slots > 0:
            # Chưa khởi động Chromium: chỉ mở ở việc render đầu tiên.
            self.browser = BrowserManager(
                worker_id=self.worker_id,
                max_pages=self.local.render_slots,
                max_body_bytes=self.config.max_body_bytes,
            )
        # Render tắt hẳn (máy chưa cài Chromium) / tạm tắt tới `_render_retry_at` (monotonic) sau
        # `_render_failures` lần liên tiếp không mở được Chromium.
        self._render_off = False
        self._render_failures = 0
        self._render_retry_at = 0.0
        self._render_reason = ""
        self._render_timer: asyncio.TimerHandle | None = None
        self._channel = channel or open_channel(settings)
        self._stub = crawl_pb2_grpc.CrawlGatewayStub(self._channel)
        self.credentials = Credentials(settings)
        self.counters = Counters()
        self._cpu = _CpuMeter()
        self._started_at_ms = now_ms()
        self.health = HealthStatus(
            settings.worker_health_file,
            self.worker_id,
            heartbeat_seconds=self.config.heartbeat_seconds,
        )
        if watchdog is None:
            watchdog = Watchdog(settings.worker_watchdog_seconds)
        self.watchdog = watchdog

        self._halt = asyncio.Event()  # thôi xin việc (dừng hoặc drain)
        self._closing = asyncio.Event()  # thôi báo sống (đã làm xong việc đang chạy)
        self._force = asyncio.Event()  # tín hiệu dừng lần hai: huỷ luôn việc đang chạy
        self._wake = asyncio.Event()  # có slot trống / đổi cấu hình / dừng
        self._config_changed = asyncio.Event()
        self.draining = False
        self._need_register = True
        self._registered_once = False
        self._http_inflight = 0
        self._render_inflight = 0
        self._tasks: set[asyncio.Task[None]] = set()
        self._jobs: dict[str, asyncio.Task[None]] = {}  # task_id → việc đang chạy
        # Việc còn nợ kết quả (đang chạy / chờ Complete): kể trong Heartbeat để gateway đối soát.
        self._owed: set[str] = set()
        self._revoked: set[str] = set()  # việc gateway đã thu hồi khi đang chạy
        self._results: asyncio.Queue[crawl_pb2.FetchResult | None] = asyncio.Queue()

    # --- điều khiển -------------------------------------------------------------------------

    def stop(self, reason: str = "signal") -> None:
        """Dừng êm: thôi xin việc, làm nốt việc đang chạy. Gọi lần hai: huỷ luôn việc đang chạy."""
        if self._halt.is_set() and not self.draining:
            log.warning("worker.force_stop", reason=reason)
            self._force.set()
            return
        if self.draining:
            self.draining = False  # đang drain mà nhận tín hiệu: coi như dừng
        log.info("worker.stopping", reason=reason, inflight=len(self._tasks))
        self._halt.set()
        self._wake.set()
        self.health.drain()

    def _begin_drain(self, source: str) -> None:
        if self._halt.is_set():
            return
        log.info("worker.drain", source=source, inflight=len(self._tasks))
        self.draining = True
        self._halt.set()
        self._wake.set()
        self.health.drain()

    async def _pause(self, seconds: float) -> None:
        """Ngủ `seconds` giây, dậy sớm khi dừng / drain."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._halt.wait(), timeout=seconds)

    def _call(self, method: Any, request: Any, *, timeout: float) -> Any:
        """Gọi RPC kèm metadata xác thực (token riêng, chưa có thì khoá tham gia); trả về call
        awaitable của grpc.aio."""
        return method(request, timeout=timeout, metadata=self.credentials.metadata())

    # --- vòng chính -------------------------------------------------------------------------

    async def run(self) -> None:
        log.info(
            "worker.start",
            worker_id=self.worker_id,
            gateway=self.settings.gateway_addr,
            pool=self.settings.worker_pool,
            http_slots=self.config.http_slots,
            render_slots=self.config.render_slots,
            local_proxies=len(self.proxies),
            watchdog_seconds=self.watchdog.limit,
            version=__version__,
        )
        self.health.write()  # `starting`: có file ngay cả khi gateway chưa lên
        self.watchdog.start()
        completer = asyncio.create_task(self._complete_loop(), name="worker-complete")
        heartbeat = asyncio.create_task(self._heartbeat_loop(), name="worker-heartbeat")
        try:
            await self._lease_loop()
        finally:
            try:
                await self._shutdown(completer, heartbeat)
            finally:
                # Trước khi rời event loop: thread kiểm tra không được thấy loop "đứng" vì đã tắt.
                await self.watchdog.stop()

    async def _lease_loop(self) -> None:
        backoff = Backoff()
        while not self._halt.is_set():
            try:
                if self._need_register:
                    await self._register()
                await self._lease_once()
                backoff.reset()
            except grpc.aio.AioRpcError as exc:
                code = exc.code()
                if code in REREGISTER_CODES:
                    log.info("worker.reregister", code=code.name, details=exc.details())
                    self._need_register = True
                    await self._pause(backoff.next())
                elif code in AUTH_CODES:
                    # Token hết hạn / khoá sai: thử khoá kế tiếp ngay, hết khoá thì chờ rồi thử lại.
                    self._need_register = True
                    if self.credentials.reject():
                        await self._pause(backoff.next())
                        continue
                    self.health.failed()
                    log.error(
                        "worker.unauthenticated",
                        code=code.name,
                        details=exc.details(),
                        retry_in=AUTH_RETRY_SECONDS,
                    )
                    await self._pause(AUTH_RETRY_SECONDS)
                else:
                    self.health.failed()
                    delay = backoff.next()
                    log.warning(
                        "worker.gateway_unavailable",
                        code=code.name,
                        details=exc.details(),
                        retry_in=round(delay, 2),
                    )
                    await self._pause(delay)
            except Exception:
                # Lỗi không lường trước: không để process chết, thử lại như mất kết nối.
                self.health.failed()
                delay = backoff.next()
                log.exception("worker.lease_error", retry_in=round(delay, 2))
                await self._pause(delay)

    async def _register(self) -> None:
        info = crawl_pb2.WorkerInfo(
            worker_id=self.worker_id,
            hostname=socket.gethostname(),
            version=__version__,
            pool=self.settings.worker_pool,
            http_slots=self.config.http_slots,
            render_slots=self._render_capacity(),
            local_proxies=len(self.proxies),
            labels=self.settings.worker_labels,
            started_at_ms=self._started_at_ms,
        )
        resp: crawl_pb2.RegisterResponse = await self._call(
            self._stub.Register,
            crawl_pb2.RegisterRequest(info=info),
            timeout=RPC_TIMEOUT_SECONDS,
        )
        if resp.worker_token:
            self.credentials.accept(resp.worker_token)
        self._apply(resp.config)
        self._need_register = False
        self._registered_once = True
        self.health.ok()
        log.info(
            "worker.registered",
            worker_id=self.worker_id,
            config_version=self.config.version,
            http_slots=self.config.http_slots,
            render_slots=self._render_capacity(),
            use_local_proxies=self.config.use_local_proxies,
        )

    def _apply(self, cfg: crawl_pb2.WorkerConfig) -> None:
        new = self.local.merge(cfg)
        old = self.config
        self.config = new
        self.health.heartbeat_seconds = new.heartbeat_seconds
        if not self._own_proxies and new.shared_hash != old.shared_hash:
            self.proxies.replace(shared_proxies(new.shared_proxies))
            log.info("worker.shared_proxies", proxies=len(self.proxies))
        if replace(old, version=new.version) != new:
            log.info(
                "worker.config",
                version=new.version,
                http_slots=new.http_slots,
                render_slots=new.render_slots,
                impersonate=new.impersonate,
                max_body_bytes=new.max_body_bytes,
                heartbeat_seconds=new.heartbeat_seconds,
                lease_wait_ms=new.lease_wait_ms,
                use_local_proxies=new.use_local_proxies,
            )
            self.http.reconfigure(
                impersonate=new.impersonate,
                max_clients=new.http_slots,
                max_body_bytes=new.max_body_bytes,
            )
            if self.browser is not None:
                self.browser.set_limits(
                    max_pages=new.render_slots, max_body_bytes=new.max_body_bytes
                )
            self._config_changed.set()
        self._wake.set()

    def _check_version(self, version: int) -> None:
        if version and version != self.config.version and not self._need_register:
            log.info("worker.config_changed", old=self.config.version, new=version)
            self._need_register = True

    def _render_paused(self) -> float:
        """Số giây còn tạm tắt render (0 = không tạm tắt)."""
        return max(0.0, self._render_retry_at - time.monotonic())

    def _render_capacity(self) -> int:
        if self.browser is None or self._render_off or self._render_paused():
            return 0
        return self.config.render_slots

    def _render_failed(self, exc: BrowserUnavailableError) -> crawl_pb2.ErrorKind:
        """Không mở được Chromium: tắt hẳn (chưa cài) hoặc tạm tắt theo backoff; Register lại để
        gateway thấy render_slots = 0. Trả ErrorKind cho kết quả của việc."""
        if exc.permanent:
            if not self._render_off:
                log.error("worker.browser_unavailable", error=str(exc), permanent=True)
                self._render_off = True
                self._render_reason = f"không có Chromium: {exc}"
                self._need_register = True
                self._wake.set()
            return crawl_pb2.ERROR_KIND_UNSUPPORTED
        # Nhiều việc render cùng chờ mở Chromium: chỉ lần lỗi đầu của đợt tính vào backoff.
        if not self._render_paused():
            self._render_failures += 1
            delay = render_backoff(self._render_failures)
            self._render_retry_at = time.monotonic() + delay
            self._render_reason = f"không mở được Chromium: {exc}"
            log.error(
                "worker.browser_unavailable",
                error=str(exc),
                failures=self._render_failures,
                retry_in=delay,
            )
            self._need_register = True
            self._wake.set()
            if self._render_timer is not None:
                self._render_timer.cancel()
            self._render_timer = asyncio.get_running_loop().call_later(delay, self._render_retry)
        return crawl_pb2.ERROR_KIND_OTHER

    def _render_retry(self) -> None:
        """Hết hạn tạm tắt: Register lại với đủ slot render, việc render sau mở lại Chromium."""
        self._render_timer = None
        if self._halt.is_set() or self._render_off:
            return
        log.info("worker.render_retry", failures=self._render_failures)
        self._need_register = True
        self._wake.set()

    def _render_succeeded(self) -> None:
        if self._render_failures:
            log.info("worker.browser_recovered", failures=self._render_failures)
        self._render_failures = 0
        self._render_retry_at = 0.0
        self._render_reason = ""

    async def _free_slots(self) -> tuple[int, int]:
        """Số slot HTTP / render còn trống; chờ tới khi có ít nhất một (hoặc dừng)."""
        while not self._halt.is_set():
            free_http = max(0, self.config.http_slots - self._http_inflight)
            free_render = max(0, self._render_capacity() - self._render_inflight)
            if free_http or free_render:
                return free_http, free_render
            self._wake.clear()
            await self._wake.wait()
        return 0, 0

    async def _lease_once(self) -> None:
        free_http, free_render = await self._free_slots()
        if self._halt.is_set() or self._need_register:
            return
        wait_ms = self.config.lease_wait_ms
        request = crawl_pb2.LeaseRequest(
            worker_id=self.worker_id,
            http_slots=free_http,
            render_slots=free_render,
            wait_ms=wait_ms,
        )
        call = asyncio.ensure_future(
            self._call(self._stub.Lease, request, timeout=wait_ms / 1000 + LEASE_EXTRA_SECONDS)
        )
        halt = asyncio.ensure_future(self._halt.wait())
        try:
            await asyncio.wait({call, halt}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            halt.cancel()
        if not call.done():
            # Dừng giữa lúc long-poll: huỷ lời gọi (việc lỡ giao trong khoảnh khắc này sẽ được
            # backend thử lại khi hết hạn).
            call.cancel()
            await asyncio.gather(call, return_exceptions=True)
            return
        resp: crawl_pb2.LeaseResponse = call.result()
        self.health.ok()
        if resp.tasks:
            log.debug("worker.lease", tasks=len(resp.tasks), http=free_http, render=free_render)
        for task in resp.tasks:
            self._spawn(task)
        self._check_version(resp.config_version)
        if resp.drain:
            self._begin_drain("lease")

    # --- chạy việc --------------------------------------------------------------------------

    def _spawn(self, task: crawl_pb2.FetchTask) -> None:
        render = task.kind == crawl_pb2.TASK_KIND_RENDER
        if render:
            self._render_inflight += 1
        else:
            self._http_inflight += 1
        job = asyncio.create_task(self._run_task(task, render), name=f"task-{task.task_id}")
        self._tasks.add(job)
        self._owed.add(task.task_id)
        self._jobs[task.task_id] = job

        def done(finished: asyncio.Task[None], task_id: str = task.task_id) -> None:
            self._tasks.discard(finished)
            self._jobs.pop(task_id, None)
            self._revoked.discard(task_id)

        job.add_done_callback(done)

    def _revoke(self, task_ids: Iterable[str]) -> None:
        """Gateway đã thu hồi các việc này (kết quả sẽ bị bỏ): huỷ việc còn đang chạy."""
        for task_id in task_ids:
            job = self._jobs.get(task_id)
            if job is None or job.done() or task_id in self._revoked:
                continue
            self._revoked.add(task_id)
            log.info("worker.task_revoked", task_id=task_id)
            job.cancel()

    def _pick_proxy(
        self, task: crawl_pb2.FetchTask, *, browser: bool
    ) -> tuple[str, str, Proxy | None]:
        """(URL proxy, proxy_id, proxy tại worker): proxy backend chỉ định → đi thẳng nếu việc
        yêu cầu → proxy tại worker gắn theo domain → đi thẳng."""
        if task.proxy:
            return task.proxy, task.proxy_id, None
        if task.direct or not self.config.use_local_proxies or not len(self.proxies):
            return "", "", None
        host = (urlsplit(task.url).hostname or "").lower()
        proxy = self.proxies.choose(host, browser=browser)
        if proxy is None:
            return "", "", None
        return proxy.url, proxy.id, proxy

    def _report_proxy(
        self, proxy: Proxy | None, task: crawl_pb2.FetchTask, result: crawl_pb2.FetchResult
    ) -> None:
        if proxy is None:
            return
        host = (urlsplit(task.url).hostname or "").lower()
        if result.error_kind in (crawl_pb2.ERROR_KIND_PROXY, crawl_pb2.ERROR_KIND_CONNECT):
            self.proxies.report_failure(proxy, host)
        elif result.status in (403, 407, 429):
            self.proxies.report_blocked(proxy, host)
        elif result.error_kind == crawl_pb2.ERROR_KIND_NONE:
            self.proxies.report_success(proxy, host)

    async def _execute(self, task: crawl_pb2.FetchTask, render: bool) -> crawl_pb2.FetchResult:
        def fail(kind: crawl_pb2.ErrorKind, error: str) -> crawl_pb2.FetchResult:
            return failure(task, kind, error, worker_id=self.worker_id)

        if expired(task):
            return fail(crawl_pb2.ERROR_KIND_EXPIRED, "quá hạn trước khi tải")
        if task.kind not in (crawl_pb2.TASK_KIND_HTTP, crawl_pb2.TASK_KIND_RENDER):
            return fail(crawl_pb2.ERROR_KIND_UNSUPPORTED, f"loại việc không hỗ trợ: {task.kind}")
        if render and (self.browser is None or self._render_off):
            return fail(crawl_pb2.ERROR_KIND_UNSUPPORTED, "worker không render (không có Chromium)")
        if render and (paused := self._render_paused()):
            return fail(
                crawl_pb2.ERROR_KIND_OTHER,
                f"render tạm tắt ({self._render_reason}), thử lại sau {paused:.0f} s",
            )
        proxy_url, proxy_id, local = self._pick_proxy(task, browser=render)
        if render and proxy_url and not browser_compatible(proxy_url):
            return fail(
                crawl_pb2.ERROR_KIND_UNSUPPORTED, "Chromium không dùng được SOCKS có mật khẩu"
            )
        try:
            async with asyncio.timeout(task_timeout(task) + TASK_GUARD_SECONDS):
                if render:
                    assert self.browser is not None
                    result = await self.browser.execute(
                        task, proxy=proxy_url or None, proxy_id=proxy_id
                    )
                    if self.browser.started:  # Chromium mở được (trang có lỗi hay không)
                        self._render_succeeded()
                else:
                    result = await self.http.execute(
                        task, proxy=proxy_url or None, proxy_id=proxy_id
                    )
        except TimeoutError:
            result = fail(crawl_pb2.ERROR_KIND_TIMEOUT, "quá thời gian chờ (worker huỷ)")
            result.proxy_id = proxy_id
        except BrowserUnavailableError as exc:
            return fail(self._render_failed(exc), f"không mở được Chromium: {exc}")
        self._report_proxy(local, task, result)
        return result

    async def _run_task(self, task: crawl_pb2.FetchTask, render: bool) -> None:
        started = time.monotonic()
        try:
            result = await self._execute(task, render)
        except asyncio.CancelledError:
            # Dừng cưỡng bức: vẫn trả kết quả để backend thử lại ngay thay vì chờ hết hạn. Việc
            # gateway thu hồi: kết quả gửi đi sẽ bị bỏ, chỉ để xoá khỏi danh sách nợ.
            revoked = task.task_id in self._revoked
            result = failure(
                task,
                crawl_pb2.ERROR_KIND_OTHER,
                "gateway đã thu hồi việc" if revoked else "worker dừng khi đang tải",
                worker_id=self.worker_id,
            )
        except Exception as exc:
            log.exception("worker.task_error", task_id=task.task_id, url=task.url)
            result = failure(
                task,
                crawl_pb2.ERROR_KIND_OTHER,
                f"{type(exc).__name__}: {exc}",
                worker_id=self.worker_id,
            )
        finally:
            if render:
                self._render_inflight -= 1
            else:
                self._http_inflight -= 1
            self._wake.set()
        if not result.duration_ms:
            result.duration_ms = int((time.monotonic() - started) * 1000)
        ok = result.error_kind == crawl_pb2.ERROR_KIND_NONE
        if render:
            self.counters.render_done += ok
            self.counters.render_failed += not ok
        else:
            self.counters.http_done += ok
            self.counters.http_failed += not ok
        log.debug(
            "worker.task_done",
            task_id=task.task_id,
            source=task.source,
            url=task.url,
            status=result.status,
            error_kind=crawl_pb2.ErrorKind.Name(result.error_kind),
            proxy_id=result.proxy_id,
            ms=result.duration_ms,
        )
        self._results.put_nowait(result)

    # --- Complete ---------------------------------------------------------------------------

    async def _complete_loop(self) -> None:
        """Gộp kết quả thành lô rồi gửi; `None` trong hàng đợi = gửi nốt rồi dừng."""
        loop = asyncio.get_running_loop()
        done = False
        while not done:
            first = await self._results.get()
            if first is None:
                return
            batch = [first]
            size = first.ByteSize()
            deadline = loop.time() + COMPLETE_FLUSH_SECONDS
            while len(batch) < COMPLETE_BATCH and size < COMPLETE_MAX_BYTES:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                try:
                    item = await asyncio.wait_for(self._results.get(), timeout=remaining)
                except TimeoutError:
                    break
                if item is None:
                    done = True
                    break
                batch.append(item)
                size += item.ByteSize()
            try:
                await self._send_results(batch)
            except Exception:
                log.exception("worker.complete_error", results=len(batch))

    async def _send_results(self, batch: list[crawl_pb2.FetchResult]) -> None:
        """Complete một lô (thử lại vài lần). Xong — gateway nhận hoặc đã bỏ cuộc — thì thôi kể các
        việc này trong Heartbeat: gateway trả `worker_lost` cho việc chưa nhận được kết quả."""
        try:
            await self._complete(batch)
        finally:
            self._owed.difference_update(result.task_id for result in batch)

    async def _complete(self, batch: list[crawl_pb2.FetchResult]) -> None:
        request = crawl_pb2.CompleteRequest(worker_id=self.worker_id, results=batch)
        for attempt in range(COMPLETE_ATTEMPTS):
            try:
                resp: crawl_pb2.CompleteResponse = await self._call(
                    self._stub.Complete, request, timeout=RPC_TIMEOUT_SECONDS
                )
            except grpc.aio.AioRpcError as exc:
                code = exc.code()
                if code in REREGISTER_CODES:
                    self._need_register = True
                if code in RETRY_CODES and attempt + 1 < COMPLETE_ATTEMPTS:
                    await asyncio.sleep(BACKOFF_BASE_SECONDS * 2**attempt)
                    continue
                log.warning(
                    "worker.complete_failed",
                    code=code.name,
                    details=exc.details(),
                    results=len(batch),
                    attempts=attempt + 1,
                )
                return
            self.health.ok()
            log.debug(
                "worker.complete", results=len(batch), accepted=resp.accepted, dropped=resp.dropped
            )
            return

    # --- Heartbeat --------------------------------------------------------------------------

    def stats(self) -> crawl_pb2.WorkerStats:
        bytes_in = self.http.bytes_in + (self.browser.bytes_in if self.browser else 0)
        paused = self._render_paused()
        reason = self._render_reason if self._render_off or paused else ""
        return crawl_pb2.WorkerStats(
            http_done=self.counters.http_done,
            http_failed=self.counters.http_failed,
            render_done=self.counters.render_done,
            render_failed=self.counters.render_failed,
            http_inflight=self._http_inflight,
            render_inflight=self._render_inflight,
            bytes_in=bytes_in,
            cpu_percent=self._cpu.percent(),
            rss_bytes=rss_bytes(),
            proxies_alive=self.proxies.alive(),
            render_disabled_reason=reason[:500],
            render_retry_at_ms=now_ms() + int(paused * 1000) if paused else 0,
        )

    async def _heartbeat_loop(self) -> None:
        loop = asyncio.get_running_loop()
        last = loop.time()
        while not self._closing.is_set():
            # Tính lại hạn mỗi lần dậy: gateway đổi `heartbeat_seconds` thì áp dụng ngay.
            remaining = last + self.config.heartbeat_seconds - loop.time()
            # Watchdog: vòng chính còn quay. Lượt sau tới muộn nhất sau thời gian chờ tới nhịp kế
            # cộng một lời gọi Heartbeat (có hạn RPC_TIMEOUT_SECONDS), kể cả lúc mất gateway.
            self.watchdog.beat(max(0.0, remaining) + RPC_TIMEOUT_SECONDS)
            if remaining > 0:
                self._config_changed.clear()
                await wait_any(remaining, self._closing, self._config_changed)
                continue
            last = loop.time()
            # Mỗi nhịp ghi lại file trạng thái (kể cả lúc chưa đăng ký được): file còn mới nghĩa
            # là event loop còn chạy; lời gọi bên dưới cập nhật trạng thái kết nối.
            self.health.touch()
            if self._need_register:
                continue
            try:
                request = crawl_pb2.HeartbeatRequest(
                    worker_id=self.worker_id,
                    stats=self.stats(),
                    task_ids=sorted(self._owed),
                    reports_tasks=True,
                )
                resp: crawl_pb2.HeartbeatResponse = await self._call(
                    self._stub.Heartbeat, request, timeout=RPC_TIMEOUT_SECONDS
                )
            except grpc.aio.AioRpcError as exc:
                if exc.code() in REREGISTER_CODES or exc.code() in AUTH_CODES:
                    # Gateway quên worker / token hết hạn: vòng xin việc Register lại.
                    log.info("worker.reregister", code=exc.code().name, details=exc.details())
                    self._need_register = True
                    self._wake.set()
                else:
                    self.health.failed()
                    log.debug("worker.heartbeat_failed", code=exc.code().name)
                continue
            except Exception:
                log.exception("worker.heartbeat_error")
                continue
            self.health.ok()
            self._revoke(resp.revoked_task_ids)
            if resp.drain:
                self._begin_drain("heartbeat")
            self._check_version(resp.config_version)

    # --- dừng -------------------------------------------------------------------------------

    def shutdown_budget(self) -> float:
        """Giây dừng êm lâu nhất dự kiến: chờ việc đang chạy, gửi nốt kết quả, Leave."""
        return (
            self.settings.worker_shutdown_grace_seconds
            + RPC_TIMEOUT_SECONDS * 2
            + LEAVE_TIMEOUT_SECONDS
        )

    async def _shutdown(self, completer: asyncio.Task[None], heartbeat: asyncio.Task[None]) -> None:
        # Vòng heartbeat sắp dừng: watchdog thôi xét vòng chính, chỉ giữ hạn tổng cho việc dừng
        # (dừng êm không bị giết, dừng treo vẫn thoát được).
        self.watchdog.drain(self.shutdown_budget())
        if self._render_timer is not None:
            self._render_timer.cancel()
            self._render_timer = None
        if self._tasks:
            grace = self.settings.worker_shutdown_grace_seconds
            log.info("worker.finishing", inflight=len(self._tasks), grace_seconds=grace)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + grace
            force = asyncio.ensure_future(self._force.wait())
            try:
                while self._tasks and not force.done():
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        break
                    await asyncio.wait(
                        {*self._tasks, force},
                        timeout=remaining,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
            finally:
                force.cancel()
            pending = list(self._tasks)
            for job in pending:
                job.cancel()
            if pending:
                log.warning("worker.cancelled_tasks", count=len(pending))
                await asyncio.gather(*pending, return_exceptions=True)
        self._results.put_nowait(None)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(completer, timeout=RPC_TIMEOUT_SECONDS * 2)
        self._closing.set()
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        if self._registered_once:
            try:
                await self._call(
                    self._stub.Leave,
                    crawl_pb2.LeaveRequest(worker_id=self.worker_id),
                    timeout=LEAVE_TIMEOUT_SECONDS,
                )
                log.info("worker.left", worker_id=self.worker_id)
            except grpc.aio.AioRpcError as exc:
                log.warning("worker.leave_failed", code=exc.code().name)
        await self.http.close()
        if self.browser is not None:
            await self.browser.close()
        await self._channel.close()
        self.health.remove()
        log.info("worker.stopped", **asdict(self.counters))


def install_signal_handlers(worker: Worker) -> None:
    """SIGTERM/SIGINT → `worker.stop()`; lần hai → huỷ việc đang chạy."""
    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, worker.stop, sig.name)
        return

    def handler(signum: int, _frame: Any) -> None:
        loop.call_soon_threadsafe(worker.stop, signal.Signals(signum).name)

    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
        if sig is not None:
            signal.signal(sig, handler)


async def run_worker(settings: WorkerSettings) -> None:
    worker = Worker(settings)
    install_signal_handlers(worker)
    await worker.run()
