"""HTTP server và HTTP proxy cục bộ (thread) cho test tích hợp, không cần mạng ngoài."""

from __future__ import annotations

import socket
import socketserver
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

Route = tuple[int, dict[str, str], bytes]


class Site:
    def __init__(self, routes: dict[str, Route | Callable[[], Route]]) -> None:
        self.routes = routes
        self.hits: list[str] = []
        self.base = ""


@contextmanager
def serve_site(routes: dict[str, Route | Callable[[], Route]]) -> Iterator[Site]:
    site = Site(routes)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            site.hits.append(self.path)
            route = site.routes.get(self.path)
            status, headers, body = (
                (route() if callable(route) else route) if route else (404, {}, b"not found")
            )
            self.send_response(status)
            for k, v in {"Content-Length": str(len(body)), **headers}.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    site.base = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield site
    finally:
        server.shutdown()
        server.server_close()


class Proxy:
    def __init__(self) -> None:
        self.requests: list[str] = []
        self.url = ""


def _read_head(conn: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


@contextmanager
def serve_proxy() -> Iterator[Proxy]:
    """Proxy HTTP tối giản: chỉ hỗ trợ request dạng absolute-form (đích http://)."""
    proxy = Proxy()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            conn: socket.socket = self.request
            head = _read_head(conn)
            if not head:
                return
            request_line, _, rest = head.partition(b"\r\n")
            method, target, version = request_line.decode().split(" ", 2)
            proxy.requests.append(target)
            parts = urlsplit(target)
            path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
            headers = [
                h
                for h in rest.split(b"\r\n")
                if h and not h.lower().startswith((b"proxy-connection", b"connection"))
            ]
            upstream = socket.create_connection((parts.hostname, parts.port or 80), timeout=5)
            with upstream:
                upstream.sendall(
                    f"{method} {path} {version}\r\n".encode()
                    + b"\r\n".join([*headers, b"Connection: close"])
                    + b"\r\n\r\n"
                )
                while chunk := upstream.recv(65536):
                    conn.sendall(chunk)

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    proxy.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield proxy
    finally:
        server.shutdown()
        server.server_close()


def closed_port() -> int:
    """Một cổng localhost hiện không có ai nghe (để giả proxy chết)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Receiver:
    """Bên nhận webhook giả: ghi mọi POST (header, body), trả lần lượt các mã trong `statuses`
    (hết danh sách thì 200)."""

    def __init__(self, statuses: list[int]) -> None:
        self.statuses = statuses
        self.requests: list[tuple[dict[str, str], bytes]] = []
        self.url = ""


@contextmanager
def serve_receiver(statuses: list[int] | None = None) -> Iterator[Receiver]:
    receiver = Receiver(list(statuses or []))

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            receiver.requests.append(({k.lower(): v for k, v in self.headers.items()}, body))
            status = receiver.statuses.pop(0) if receiver.statuses else 200
            self.send_response(status)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    receiver.url = f"http://127.0.0.1:{server.server_address[1]}/hooks/jobs?token=bi-mat"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield receiver
    finally:
        server.shutdown()
        server.server_close()
