"""Ứng dụng web (FastAPI): API JSON + phục vụ giao diện React (frontend/, build vào `dist/`).

`jobcrawler ui` hoặc service `api` trong compose. Dùng chung `open_resources` và các hàm trong
`jobcrawler.services` như CLI; worker vẫn là process riêng, giao diện chỉ đưa job vào queue.

Middleware, từ ngoài vào: `RequestIdMiddleware` (request id, log truy cập, lỗi chưa xử lý thành 500)
→ `GuardMiddleware` (đăng nhập HTTP Basic, chặn đoán mật khẩu, chặn CSRF) → FastAPI. Mọi lỗi trả
về cùng dạng JSON `{"detail": ...}` như `HTTPException` của FastAPI.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from jobcrawler.config import Settings, get_settings
from jobcrawler.db.ids import uuid7
from jobcrawler.logging import configure_logging, get_logger
from jobcrawler.resources import open_resources
from jobcrawler.web import api, settings_api

DEFAULT_DIST = Path(__file__).with_name("dist")
NO_BUILD = """<!doctype html><meta charset="utf-8"><title>jobcrawler</title>
<body style="font-family:system-ui;max-width:640px;margin:40px auto;line-height:1.6">
<h1>Chưa build giao diện</h1>
<p>Chạy <code>cd frontend &amp;&amp; npm install &amp;&amp; npm run build</code> rồi tải lại trang,
hoặc chạy dev server <code>npm run dev</code> (cổng 5173). API vẫn dùng được ở
<a href="/api/docs">/api/docs</a>.</p></body>"""

log = get_logger(__name__)

PUBLIC_PATHS = ("/healthz",)
_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}

REQUEST_ID_HEADER = "X-Request-Id"
# Log truy cập chỉ cho API: file tĩnh, trang SPA, /healthz (healthcheck vài giây một lần) là nhiễu.
_ACCESS_LOG_PREFIX = "/api/"
# File trong `assets/` của bản build Vite có hash nội dung trong tên: cache một năm, không hỏi lại.
ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"
# Chặn đoán mật khẩu: sai LOGIN_FAIL_LIMIT lần, mỗi lần cách lần trước không quá LOGIN_FAIL_WINDOW
# giây (mỗi lần sai gia hạn bộ đếm), thì IP đó nhận 429 tới khi bộ đếm hết hạn.
LOGIN_FAIL_LIMIT = 20
LOGIN_FAIL_WINDOW = 300


def json_error(
    status_code: int, detail: str, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    """Lỗi cùng dạng với `HTTPException` của FastAPI (giao diện đọc `detail`)."""
    return JSONResponse({"detail": detail}, status_code=status_code, headers=headers)


def server_error(request_id: str) -> JSONResponse:
    """500 cho lỗi chưa xử lý: không lộ chi tiết, kèm request id để tìm log."""
    return JSONResponse({"detail": "Lỗi máy chủ", "request_id": request_id}, status_code=500)


def _request_id(headers: Headers) -> str:
    """`X-Request-Id` của client nếu là UUID (đưa về dạng chuẩn), không thì sinh UUIDv7 mới.

    Chỉ nhận UUID: request id được ghi vào log và cột uuid của nhật ký thao tác.
    """
    incoming = headers.get(REQUEST_ID_HEADER)
    if incoming and len(incoming) <= 64:
        try:
            return str(UUID(incoming))
        except ValueError:
            pass
    return str(uuid7())


class RequestIdMiddleware:
    """Ngoài cùng: gắn request id, ghi log truy cập API, đổi lỗi chưa xử lý thành 500 JSON.

    Request id đọc được trong lúc xử lý request qua
    `structlog.contextvars.get_contextvars()["request_id"]` (mọi log của request tự kèm) và
    `request.state.request_id`; trả về client ở header `X-Request-Id`. Mỗi request bắt đầu với
    contextvars của structlog trống.

    Lỗi chưa xử lý được bắt ở đây chứ không bằng `app.exception_handler(Exception)`: Starlette chạy
    handler đó ở `ServerErrorMiddleware`, ngoài mọi middleware (response 500 thiếu `X-Request-Id`,
    log truy cập không thấy status) rồi ném lại lỗi để uvicorn in traceback lần nữa.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = _request_id(Headers(scope=scope))
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        state: dict[str, Any] = scope.setdefault("state", {})
        state["request_id"] = request_id
        method, path = scope["method"], scope["path"]
        started = time.perf_counter()
        status = 500
        response_started = False

        async def send_with_id(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                status = message["status"]
                response_started = True
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        except Exception:
            log.exception("http.unhandled", method=method, path=path)
            if response_started:
                raise  # đã gửi một phần response: để server đóng kết nối
            await server_error(request_id)(scope, receive, send_with_id)
        finally:
            if path.startswith(_ACCESS_LOG_PREFIX):
                log.info(
                    "http.request",
                    method=method,
                    path=path,
                    status=status,
                    duration_ms=round((time.perf_counter() - started) * 1000, 1),
                    actor=state.get("user"),
                )


class AuthRedis(Protocol):
    """Phần Redis mà `LoginThrottle` dùng (`redis.asyncio.Redis`; test dùng bản giả)."""

    def get(self, name: str) -> Awaitable[Any]: ...
    def incr(self, name: str) -> Awaitable[Any]: ...
    def expire(self, name: str, time: int) -> Awaitable[Any]: ...
    def ttl(self, name: str) -> Awaitable[Any]: ...
    def delete(self, *names: str) -> Awaitable[Any]: ...


class LoginThrottle:
    """Chặn đoán mật khẩu HTTP Basic theo IP, đếm trên Redis (dùng chung mọi process API).

    Khoá `<redis_prefix>:auth:fail:<ip>`: mỗi lần sai INCR rồi EXPIRE `window` giây; đủ `limit`
    lần thì mọi request từ IP đó (kể cả mật khẩu đúng) nhận 429 kèm `Retry-After` tới khi khoá hết
    hạn; đăng nhập đúng thì xoá bộ đếm. Request không gửi thông tin đăng nhập (lần đầu mở trang,
    trình duyệt chưa hỏi mật khẩu) không bị tính.

    IP là `request.client.host`. Sau reverse proxy, uvicorn chỉ lấy IP thật từ `X-Forwarded-For`
    khi chạy với `proxy_headers` (`jobcrawler ui` bật sẵn) và IP của proxy nằm trong
    `--forwarded-allow-ips` / `FORWARDED_ALLOW_IPS` (mặc định 127.0.0.1); không thì mọi người dùng
    chung IP của proxy, tức chung một bộ đếm.

    Redis lỗi hay chưa mở (`redis()` trả None) thì cho qua, chỉ log cảnh báo: Redis trục trặc không
    được khoá mọi người ở ngoài.
    """

    def __init__(
        self,
        redis: Callable[[], AuthRedis | None],
        *,
        prefix: str,
        limit: int = LOGIN_FAIL_LIMIT,
        window: int = LOGIN_FAIL_WINDOW,
    ) -> None:
        self._redis = redis
        self._prefix = prefix
        self.limit = limit
        self.window = window

    def key(self, ip: str) -> str:
        return f"{self._prefix}:auth:fail:{ip}"

    async def failures(self, ip: str) -> int:
        """Số lần sai đang tính của `ip` (0 khi Redis lỗi)."""
        redis = self._redis()
        if redis is None:
            return 0
        try:
            raw = await redis.get(self.key(ip))
            return 0 if raw is None else int(raw)
        except Exception as exc:
            log.warning("auth.throttle_unavailable", op="get", error=repr(exc))
            return 0

    async def retry_after(self, ip: str) -> int:
        """Số giây còn bị chặn (TTL của khoá); khoá mất TTL thì đặt lại để không khoá mãi."""
        redis = self._redis()
        if redis is None:
            return self.window
        key = self.key(ip)
        try:
            ttl = int(await redis.ttl(key))
            if ttl < 0:
                await redis.expire(key, self.window)
                ttl = self.window
        except Exception as exc:
            log.warning("auth.throttle_unavailable", op="ttl", error=repr(exc))
            return self.window
        return max(ttl, 1)

    async def failed(self, ip: str) -> int:
        """Tính thêm một lần sai; trả số lần sai hiện tại (0 khi Redis lỗi)."""
        redis = self._redis()
        if redis is None:
            return 0
        key = self.key(ip)
        try:
            count = int(await redis.incr(key))
            await redis.expire(key, self.window)
        except Exception as exc:
            log.warning("auth.throttle_unavailable", op="incr", error=repr(exc))
            return 0
        return count

    async def succeeded(self, ip: str) -> None:
        redis = self._redis()
        if redis is None:
            return
        try:
            await redis.delete(self.key(ip))
        except Exception as exc:
            log.warning("auth.throttle_unavailable", op="delete", error=repr(exc))


def _client_ip(scope: Scope) -> str:
    client = scope.get("client")
    return client[0] if client else "unknown"


class GuardMiddleware:
    """Đăng nhập HTTP Basic (khi có UI_PASSWORD) và chặn request ghi từ site khác (CSRF: trình
    duyệt tự gửi kèm thông tin đăng nhập Basic).

    Người dùng đã đăng nhập ở `request.state.user` (và `actor` trong contextvars của structlog để
    log của request kèm theo). `throttle`: chặn đoán mật khẩu (chỉ khi có mật khẩu).
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        username: str,
        password: str | None,
        throttle: LoginThrottle | None = None,
    ) -> None:
        self.app = app
        self.username = username
        self.password = password
        self.throttle = throttle if password is not None else None

    def _user(self, headers: Headers) -> str | None:
        if self.password is None:
            return self.username
        value = headers.get("authorization", "")
        scheme, _, token = value.partition(" ")
        if scheme.lower() != "basic":
            return None
        try:
            user, _, password = base64.b64decode(token).decode().partition(":")
        except (binascii.Error, UnicodeDecodeError):
            return None
        ok = secrets.compare_digest(user.encode(), self.username.encode()) & secrets.compare_digest(
            password.encode(), self.password.encode()
        )
        return user if ok else None

    @staticmethod
    def _same_origin(headers: Headers) -> bool:
        host = headers.get("host", "")
        source = headers.get("origin") or headers.get("referer")
        if not source:
            return True  # client không phải trình duyệt (curl, script): không bị CSRF
        return urlsplit(source).netloc == host

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        throttle, ip = self.throttle, _client_ip(scope)
        failures = await throttle.failures(ip) if throttle else 0
        if throttle and failures >= throttle.limit:
            # Chặn cả mật khẩu đúng: không thì đoán tiếp vẫn biết lúc nào trúng.
            retry = await throttle.retry_after(ip)
            response = json_error(
                429, "Đăng nhập sai quá nhiều lần, thử lại sau", {"Retry-After": str(retry)}
            )
            await response(scope, receive, send)
            return
        user = self._user(headers)
        if user is None:
            if throttle and headers.get("authorization"):
                count = await throttle.failed(ip)
                if count == throttle.limit:
                    log.warning("auth.locked", ip=ip, failures=count, seconds=throttle.window)
            response = json_error(
                401,
                "Cần đăng nhập",
                {"WWW-Authenticate": 'Basic realm="jobcrawler", charset="UTF-8"'},
            )
            await response(scope, receive, send)
            return
        if throttle and failures:
            await throttle.succeeded(ip)
        if scope["method"] in _UNSAFE and not self._same_origin(headers):
            await json_error(403, "Request từ site khác bị chặn")(scope, receive, send)
            return
        scope.setdefault("state", {})["user"] = user
        structlog.contextvars.bind_contextvars(actor=user)
        await self.app(scope, receive, send)


def operation_id(route: APIRoute) -> str:
    """`operationId` trong spec = tên hàm xử lý (`get_company_jobs`): ổn định, dễ đọc (mặc định của
    FastAPI ghép thêm đường dẫn + method). Tên hàm trong `web/api.py`, `web/settings_api.py` không
    được trùng nhau (test kiểm)."""
    return route.name


class AssetFiles(StaticFiles):
    """`/assets` của bản build: tên file có hash nội dung nên cho trình duyệt cache lâu."""

    def file_response(
        self,
        full_path: str | os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code)
        response.headers["Cache-Control"] = ASSET_CACHE_CONTROL
        return response


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    # uvicorn --reload chạy app ở process con: cấu hình log lại ở đây.
    configure_logging(settings)
    # Log truy cập của uvicorn trùng `http.request` (có request id, thời gian xử lý, người dùng)
    # và in cả từng file tĩnh: chỉ giữ cảnh báo.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with open_resources(settings, db_logs=False) as res:
            app.state.res = res
            if settings.ui_password is None:
                log.warning("ui.no_password", reason="chưa đặt UI_PASSWORD: không hỏi đăng nhập")
            yield

    app = FastAPI(
        title="jobcrawler",
        description="Giao diện vận hành và API của jobcrawler.",
        lifespan=lifespan,
        docs_url="/api/docs",
        # Bản commit ở frontend/openapi.json (scripts/export_openapi.py), giao diện sinh kiểu từ đó.
        openapi_url="/api/openapi.json",
        redoc_url=None,
        generate_unique_id_function=operation_id,
    )
    app.state.settings = settings

    def redis() -> AuthRedis | None:
        res = getattr(app.state, "res", None)  # có sau khi lifespan mở tài nguyên
        return None if res is None else res.redis

    password = settings.ui_password.get_secret_value() if settings.ui_password else None
    throttle = LoginThrottle(redis, prefix=settings.redis_prefix)
    app.add_middleware(
        GuardMiddleware, username=settings.ui_username, password=password, throttle=throttle
    )
    app.add_middleware(RequestIdMiddleware)  # thêm sau cùng = ngoài cùng

    @app.exception_handler(RequestValidationError)
    async def bad_id(request: Request, exc: RequestValidationError) -> Response:
        # Id trong đường dẫn không phải UUID (link cũ dùng id số trước migration 0007): coi như
        # không có, thay vì lỗi kiểm tra dữ liệu.
        errors = exc.errors()
        if errors and all(e["loc"][0] == "path" and e["type"] == "uuid_parsing" for e in errors):
            return JSONResponse({"detail": "Không tìm thấy (đường dẫn cũ hoặc sai id)"}, 404)
        return await request_validation_exception_handler(request, exc)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> PlainTextResponse:
        return PlainTextResponse("ok")

    app.include_router(api.router)
    app.include_router(settings_api.router)
    dist = settings.ui_dist or DEFAULT_DIST
    if (dist / "assets").is_dir():
        app.mount("/assets", AssetFiles(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str) -> Response:
        """Giao diện React: file tĩnh nếu có, còn lại trả index.html (router phía trình duyệt)."""
        if path.startswith("api/"):
            raise HTTPException(404, "Không có API này")
        index = dist / "index.html"
        if not index.is_file():
            return HTMLResponse(NO_BUILD, status_code=503)
        target = (dist / path).resolve()
        if path and target.is_file() and dist.resolve() in target.parents:
            return FileResponse(target)
        # index.html không cache để bản build mới có hiệu lực ngay (file trong assets có hash).
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return app


def openapi_document() -> str:
    """Spec như `/api/openapi.json`, dạng JSON khoá sắp xếp (diff gọn): bản commit ở
    frontend/openapi.json (`scripts/export_openapi.py`). Không mở Postgres / Redis (lifespan không
    chạy), không đọc .env: spec chỉ phụ thuộc mã."""
    spec = create_app(Settings(_env_file=None)).openapi()  # pyright: ignore[reportCallIssue]
    return json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
