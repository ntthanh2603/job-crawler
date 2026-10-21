"""Gateway gRPC giả (trong process) + web server và forward proxy HTTP nhỏ cho test worker."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import http.client
import json
import random
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import grpc

from jcproto.v1 import crawl_pb2, crawl_pb2_grpc

# --- gateway giả ---------------------------------------------------------------------------------


class FakeGateway(crawl_pb2_grpc.CrawlGatewayServicer):
    """Giữ hàng việc, gom kết quả; cờ để giả lập drain / quên worker / đổi cấu hình / token."""

    def __init__(self) -> None:
        self.config = crawl_pb2.WorkerConfig(version=1, lease_wait_ms=200, use_local_proxies=True)
        self.pending: list[crawl_pb2.FetchTask] = []
        self.results: list[crawl_pb2.FetchResult] = []
        self.registers: list[crawl_pb2.RegisterRequest] = []
        self.leases: list[crawl_pb2.LeaseRequest] = []
        self.heartbeats: list[crawl_pb2.HeartbeatRequest] = []
        self.leaves: list[str] = []
        self.metadata: list[dict[str, str]] = []
        self.known: set[str] = set()
        self.drain = False
        self.revoke: set[str] = set()  # task_id gateway báo đã thu hồi (khi worker kể ở Heartbeat)
        self.not_found_leases = 0  # số lần Lease tiếp theo trả NOT_FOUND
        # Khoá tham gia (GATEWAY_TOKEN / khoá một lần). `worker_token`: đặt thì Register trả token
        # riêng này và mọi lời gọi khác phải dùng nó (như gateway thật khi bật xác thực).
        self.token: str | None = None
        self.worker_token: str | None = None
        self.auth_failures = 0
        # Như gateway thật: token riêng gắn với worker_id vừa Register; lời gọi khác bằng token đó
        # với id khác → PERMISSION_DENIED (process khác dùng chung file token vừa Register).
        self.token_owner: str | None = None
        self.permission_denied = 0
        self.register_errors: list[grpc.StatusCode] = []  # mã lỗi cho các lần Register tới
        self.complete_errors: list[grpc.StatusCode] = []  # mã lỗi cho các lần Complete tới
        self.completes = 0  # số lời gọi Complete (kể cả lỗi)
        self._changed = asyncio.Event()

    # --- điều khiển từ test ---

    def add(self, *tasks: crawl_pb2.FetchTask) -> None:
        self.pending.extend(tasks)
        self._changed.set()

    def forget(self) -> None:
        """Gateway khởi động lại: không còn biết worker nào."""
        self.known.clear()

    async def wait_for(self, predicate: Callable[[], bool], timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                raise AssertionError("hết thời gian chờ điều kiện trên gateway giả")
            await asyncio.sleep(0.02)

    async def wait_results(self, n: int, timeout: float = 15.0) -> list[crawl_pb2.FetchResult]:
        await self.wait_for(lambda: len(self.results) >= n, timeout)
        return self.results

    def result(self, task_id: str) -> crawl_pb2.FetchResult:
        return next(r for r in self.results if r.task_id == task_id)

    # --- RPC ---

    async def _auth(
        self, context: grpc.aio.ServicerContext, worker_id: str = "", *, register: bool = False
    ) -> None:
        md = {str(k): str(v) for k, v in context.invocation_metadata() or ()}
        self.metadata.append(md)
        if self.token is None and self.worker_token is None:
            return
        if self.worker_token is None:
            allowed = {self.token}
        elif register:
            allowed = {self.token, self.worker_token}
        else:
            allowed = {self.worker_token}
        if md.get("authorization") not in {f"Bearer {a}" for a in allowed if a}:
            self.auth_failures += 1
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "token sai")
        if not register and self.token_owner not in (None, worker_id):
            self.permission_denied += 1
            await context.abort(grpc.StatusCode.PERMISSION_DENIED, "token thuộc worker khác")

    async def Register(self, request: Any, context: Any) -> crawl_pb2.RegisterResponse:
        await self._auth(context, register=True)
        if self.register_errors:
            await context.abort(self.register_errors.pop(0), "Register bị từ chối")
        self.registers.append(request)
        self.known.add(request.info.worker_id)
        if self.worker_token is not None:
            self.token_owner = request.info.worker_id
        return crawl_pb2.RegisterResponse(config=self.config, worker_token=self.worker_token or "")

    async def Lease(self, request: Any, context: Any) -> crawl_pb2.LeaseResponse:
        await self._auth(context, request.worker_id)
        if self.not_found_leases > 0:
            self.not_found_leases -= 1
            await context.abort(grpc.StatusCode.NOT_FOUND, "worker không rõ")
        if request.worker_id not in self.known:
            await context.abort(grpc.StatusCode.NOT_FOUND, "worker không rõ")
        self.leases.append(request)
        deadline = time.monotonic() + request.wait_ms / 1000
        while True:
            tasks = [] if self.drain else self._take(request.http_slots, request.render_slots)
            if tasks or self.drain or time.monotonic() >= deadline:
                break
            self._changed.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), deadline - time.monotonic())
        return crawl_pb2.LeaseResponse(
            tasks=tasks, config_version=self.config.version, drain=self.drain
        )

    def _take(self, http_slots: int, render_slots: int) -> list[crawl_pb2.FetchTask]:
        taken: list[crawl_pb2.FetchTask] = []
        for task in list(self.pending):
            # Việc render ép giao cho worker (kể cả khi worker xin 0) để test UNSUPPORTED.
            forced = task.source == "force"
            if task.kind == crawl_pb2.TASK_KIND_RENDER:
                if render_slots <= 0 and not forced:
                    continue
                render_slots -= 1
            else:
                if http_slots <= 0:
                    continue
                http_slots -= 1
            taken.append(task)
            self.pending.remove(task)
        return taken

    async def Complete(self, request: Any, context: Any) -> crawl_pb2.CompleteResponse:
        self.completes += 1
        await self._auth(context, request.worker_id)
        if self.complete_errors:
            await context.abort(self.complete_errors.pop(0), "Complete lỗi")
        self.results.extend(request.results)
        return crawl_pb2.CompleteResponse(accepted=len(request.results))

    async def Heartbeat(self, request: Any, context: Any) -> crawl_pb2.HeartbeatResponse:
        await self._auth(context, request.worker_id)
        if request.worker_id not in self.known:
            await context.abort(grpc.StatusCode.NOT_FOUND, "worker không rõ")
        self.heartbeats.append(request)
        revoked = [t for t in request.task_ids if t in self.revoke]
        return crawl_pb2.HeartbeatResponse(
            config_version=self.config.version, drain=self.drain, revoked_task_ids=revoked
        )

    async def Leave(self, request: Any, context: Any) -> crawl_pb2.LeaveResponse:
        await self._auth(context, request.worker_id)
        self.leaves.append(request.worker_id)
        self.known.discard(request.worker_id)
        return crawl_pb2.LeaveResponse()


async def start_gateway(gateway: FakeGateway) -> tuple[grpc.aio.Server, int]:
    server = grpc.aio.server()
    crawl_pb2_grpc.add_CrawlGatewayServicer_to_server(gateway, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    return server, port


def http_task(task_id: str, url: str, **kw: Any) -> crawl_pb2.FetchTask:
    kw.setdefault("method", "GET")
    kw.setdefault("timeout_seconds", 10)
    kw.setdefault("allow_redirects", True)
    return crawl_pb2.FetchTask(task_id=task_id, kind=crawl_pb2.TASK_KIND_HTTP, url=url, **kw)


def render_task(task_id: str, url: str, **kw: Any) -> crawl_pb2.FetchTask:
    kw.setdefault("method", "GET")
    kw.setdefault("timeout_seconds", 20)
    kw.setdefault("wait_until", "load")
    return crawl_pb2.FetchTask(task_id=task_id, kind=crawl_pb2.TASK_KIND_RENDER, url=url, **kw)


# --- web server ----------------------------------------------------------------------------------

PAGE = ("<html><head><title>Trang</title></head><body>" + "<p>việc làm</p>" * 400).encode()
BIG_BYTES = 256 * 1024
NOISE = random.Random(0).randbytes(64 * 1024)  # không nén được: kết quả vẫn lớn sau zstd
SPA = b"""<html><body><div id="out">loading</div><script>
fetch('/api/jobs', {method: 'POST', headers: {'content-type': 'application/json'},
                    body: JSON.stringify({q: 'x'})})
  .then(r => r.json())
  .then(d => { document.getElementById('out').textContent = 'jobs:' + d.jobs.length; });
</script></body></html>"""
JOBS = json.dumps({"jobs": [{"title": "Kỹ sư"}, {"title": "Kế toán"}]}).encode()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _WebServer

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _send(self, status: int, body: bytes, ctype: str = "text/html; charset=utf-8", **h: str):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Test", "Yes")
        for k, v in h.items():
            self.send_header(k.replace("_", "-"), v)
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def do_GET(self) -> None:
        self._route()

    def do_POST(self) -> None:
        self._route()

    def _route(self) -> None:
        path = urlsplit(self.path).path
        with self.server.lock:
            self.server.hits[path] += 1
        body = self._body()
        if path == "/page":
            self._send(200, PAGE)
        elif path == "/small":
            self._send(200, b"ok")
        elif path == "/trace":  # như https://www.cloudflare.com/cdn-cgi/trace
            self._send(200, b"fl=1\nip=127.0.0.1\nloc=VN\n", "text/plain")
        elif path == "/redirect":
            self._send(302, b"", Location="/page")
        elif path == "/slow":
            time.sleep(3)
            self._send(200, b"cham")
        elif path == "/big":
            self._send(200, b"x" * BIG_BYTES, "text/plain")
        elif path == "/noise":
            self._send(200, NOISE, "application/octet-stream")
        elif path == "/big-chunked":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            chunk = b"y" * 16384
            try:
                for _ in range(BIG_BYTES // len(chunk)):
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                self.wfile.write(b"0\r\n\r\n")
            except OSError:
                pass
        elif path == "/429":
            self._send(429, b"cham lai", Retry_After="5")
        elif path == "/403":
            self._send(403, b"cam")
        elif path == "/echo":
            data = {
                "method": self.command,
                "body": body.decode(),
                "headers": {k.lower(): v for k, v in self.headers.items()},
            }
            self._send(200, json.dumps(data).encode(), "application/json")
        elif path == "/spa":
            self._send(200, SPA)
        elif path == "/api/jobs":
            self._send(200, JOBS, "application/json")
        else:
            self._send(404, b"khong co")


class _WebServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass  # client bỏ ngang (timeout, quá lớn): không in traceback

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.hits: Counter[str] = Counter()
        self.lock = threading.Lock()


class WebServer:
    """Web server thật (thread) cho test: `url("/page")`, `hits["/page"]`."""

    def __init__(self) -> None:
        self._server = _WebServer()
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def hits(self) -> Counter[str]:
        return self._server.hits

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self._server.server_port}{path}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


# --- forward proxy HTTP --------------------------------------------------------------------------


class _ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: _ProxyServer

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _relay(self) -> None:
        expected = self.server.auth
        if expected is not None and self.headers.get("Proxy-Authorization") != expected:
            self.send_response(407)
            self.send_header("Proxy-Authenticate", 'Basic realm="test"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        with self.server.lock:
            self.server.requests.append(self.path)
        target = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {
            k: v
            for k, v in self.headers.items()
            if not k.lower().startswith("proxy-") and k.lower() not in ("connection", "host")
        }
        conn = http.client.HTTPConnection(target.hostname or "", target.port or 80, timeout=10)
        path = target.path + (f"?{target.query}" if target.query else "")
        conn.request(self.command, path or "/", body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() not in ("transfer-encoding", "connection", "content-length"):
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        conn.close()

    do_GET = _relay
    do_POST = _relay


class _ProxyServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass  # client bỏ ngang (timeout, quá lớn): không in traceback

    def __init__(self, auth: str | None) -> None:
        super().__init__(("127.0.0.1", 0), _ProxyHandler)
        self.auth = auth
        self.requests: list[str] = []
        self.lock = threading.Lock()


class ForwardProxy:
    """Proxy HTTP (chỉ URL http://, không CONNECT), tuỳ chọn yêu cầu Basic auth."""

    def __init__(self, user: str | None = None, password: str | None = None) -> None:
        auth = None
        if user is not None:
            token = base64.b64encode(f"{user}:{password or ''}".encode()).decode()
            auth = f"Basic {token}"
        self._user = user
        self._password = password
        self._server = _ProxyServer(auth)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def requests(self) -> list[str]:
        return self._server.requests

    @property
    def url(self) -> str:
        creds = f"{self._user}:{self._password}@" if self._user is not None else ""
        return f"http://{creds}127.0.0.1:{self._server.server_port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def closed_port() -> int:
    """Một port vừa đóng trên 127.0.0.1 (kết nối tới sẽ bị từ chối)."""
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- worker ---------------------------------------------------------------------------------------


def make_settings(port: int, **kw: Any) -> Any:
    """WorkerSettings cho test: không đọc .env, không Chromium, không watchdog thật (dừng ở
    debugger quá hạn thì `os._exit` giết luôn pytest) — trừ khi ghi đè."""
    from jobworker.common.config import WorkerSettings

    values: dict[str, Any] = {
        "gateway_addr": f"127.0.0.1:{port}",
        "worker_id": "test-worker",
        "worker_http_slots": 4,
        "worker_render_slots": 0,
        "worker_shutdown_grace_seconds": 10,
        "worker_watchdog_seconds": 0,
        # Không ghi token vào thư mục home của máy chạy test.
        "worker_token_file": Path(tempfile.mkdtemp(prefix="jobworker-test-")) / "token",
    }
    values.update(kw)
    return WorkerSettings(_env_file=None, **values)  # pyright: ignore[reportCallIssue]


class RunningWorker:
    """Chạy `Worker.run()` nền; `stop()` dừng êm và chờ thoát."""

    def __init__(self, worker: Any) -> None:
        self.worker = worker
        self.task = asyncio.create_task(worker.run())

    async def wait(self, timeout: float = 20.0) -> None:
        await asyncio.wait_for(self.task, timeout=timeout)

    async def stop(self, timeout: float = 20.0) -> None:
        if not self.task.done():
            self.worker.stop("test")
        await self.wait(timeout)
