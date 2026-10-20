"""Ứng dụng web (FastAPI): API JSON (`/api/...`) và `/healthz`. Giao diện React là service riêng
(nginx phục vụ bản build, chỉ chuyển `/api/` tới đây).

`jobcrawler api` (trong compose: một process của container `backend`). Dùng chung
`open_resources` và các hàm trong `jobcrawler.services` như CLI; worker vẫn là process riêng, giao
diện chỉ đưa job vào queue.

Middleware, từ ngoài vào: `RequestIdMiddleware` (request id, log truy cập, lỗi chưa xử lý thành 500)
→ `GuardMiddleware` (đăng nhập: header của proxy SSO hoặc HTTP Basic, chặn đoán mật khẩu, chặn
CSRF) → FastAPI. Mọi lỗi trả về cùng dạng JSON `{"detail": ...}` như `HTTPException` của FastAPI.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any, Protocol
from urllib.parse import urlsplit
from uuid import UUID

import structlog
from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from jobcrawler.config import Settings, get_settings
from jobcrawler.db.ids import uuid7
from jobcrawler.logging import configure_logging, get_logger
from jobcrawler.resources import open_resources
from jobcrawler.watchdog import start_watchdog
from jobcrawler.web import api, settings_api
from jobcrawler.web import auth as auth_tokens
from jobcrawler.web.auth import TokenAuth

log = get_logger(__name__)

# Không qua đăng nhập giao diện: healthcheck.
# Không qua đăng nhập: healthcheck, đăng nhập / đăng xuất (tự đọc token).
PUBLIC_PATHS = ("/healthz", "/api/auth/login", "/api/auth/logout")
# Giá trị header người dùng của proxy SSO dài hơn chừng này thì cắt (ghi vào log / nhật ký).
MAX_PROXY_USER_CHARS = 256
_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
BEARER_CHALLENGE = 'Bearer realm="jobcrawler"'

REQUEST_ID_HEADER = "X-Request-Id"
# Log truy cập chỉ cho API: /healthz (healthcheck vài giây một lần) là nhiễu.
_ACCESS_LOG_PREFIX = "/api/"
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
    khi chạy với `proxy_headers` (`jobcrawler api` bật sẵn) và IP của proxy nằm trong
    `--forwarded-allow-ips` / `FORWARDED_ALLOW_IPS` (mặc định 127.0.0.1,::1); không thì mọi người
    dùng chung IP của proxy (nginx của giao diện), tức chung một bộ đếm.

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
    """Đăng nhập và chặn request ghi từ site khác (CSRF: trình duyệt tự gửi kèm cookie phiên).

    Ba chế độ (`Settings.auth_mode`):

    - `proxy` (`proxy_user_header`, AUTH_PROXY_USER_HEADER): SSO qua proxy tin cậy (oauth2-proxy
      đứng trước service giao diện) — người dùng lấy từ header đó, trống thì từ
      `proxy_email_header`; thiếu cả hai thì 401 JSON. Không có mật khẩu để đoán.
      BẢO MẬT: chỉ an toàn khi không ai gọi thẳng được API (cổng api không mở ra ngoài; mọi
      request đi qua nginx giao diện sau oauth2-proxy, proxy ghi đè header này) — gọi thẳng được
      thì ai cũng tự đặt header và đăng nhập thành bất kỳ ai.
    - `jwt` (UI_PASSWORD): JWT của `POST /api/auth/login` trong cookie `jc_token` hoặc header
      `Authorization: Bearer` (`web/auth.py`): đúng chữ ký, còn hạn, chưa thu hồi. Thiếu / sai thì
      401 JSON kèm `WWW-Authenticate: Bearer` (giao diện chuyển tới trang đăng nhập).
    - `none`: không hỏi đăng nhập, người dùng là `username`.

    Người dùng đã đăng nhập ở `request.state.user` (và `actor` trong contextvars của structlog để
    log của request kèm theo; nhật ký thao tác ghi đúng người).
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        username: str,
        token_auth: Callable[[], TokenAuth | None] | None = None,
        redis: Callable[[], AuthRedis | None] | None = None,
        prefix: str = "jc",
        proxy_user_header: str | None = None,
        proxy_email_header: str | None = None,
    ) -> None:
        self.app = app
        self.username = username
        self.token_auth = token_auth
        self.redis = redis or (lambda: None)
        self.prefix = prefix
        self.proxy_user_header = (proxy_user_header or "").strip() or None
        self.proxy_email_header = (proxy_email_header or "").strip() or None

    def _proxy_user(self, headers: Headers) -> str | None:
        """Người dùng do proxy SSO gắn: header người dùng, trống thì header email."""
        for name in (self.proxy_user_header, self.proxy_email_header):
            if name and (value := headers.get(name, "").strip()):
                return value[:MAX_PROXY_USER_CHARS]
        return None

    async def _token_user(self, headers: Headers) -> str | None:
        auth = self.token_auth() if self.token_auth else None
        token = auth_tokens.token_from(headers)
        if auth is None or token is None:
            return None
        claims = auth.verify(token)
        if claims is None:
            return None
        redis = self.redis()
        if redis is not None:
            try:
                if await auth_tokens.is_revoked(redis, self.prefix, claims.jti):
                    return None
            except Exception as exc:  # Redis lỗi: không khoá mọi người ở ngoài
                log.warning("auth.revoked_check_failed", error=str(exc))
        return claims.user

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
        if self.proxy_user_header is not None:
            user = self._proxy_user(headers)
            if user is None:
                await json_error(401, "Cần đăng nhập SSO")(scope, receive, send)
                return
        elif self.token_auth is not None:
            user = await self._token_user(headers)
            if user is None:
                # `Bearer`: trình duyệt không bật hộp đăng nhập; giao diện nhận ra để chuyển tới
                # trang đăng nhập (khác 401 của SSO).
                challenge = {"WWW-Authenticate": BEARER_CHALLENGE}
                await json_error(401, "Cần đăng nhập", challenge)(scope, receive, send)
                return
        else:
            user = self.username
        await self._pass(scope, receive, send, headers, user)

    async def _pass(
        self, scope: Scope, receive: Receive, send: Send, headers: Headers, user: str
    ) -> None:
        """Đã biết người dùng: chặn CSRF rồi cho request đi tiếp, kèm người dùng."""
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


async def unknown_path(scope: Scope, receive: Receive, send: Send) -> None:
    """Không route nào khớp đường dẫn (khác method thì router vẫn trả 405): 404 cùng dạng JSON,
    lời tiếng Việt như mọi lỗi khác của API."""
    if scope["type"] == "websocket":  # API không có websocket: đóng như router mặc định
        await WebSocketClose()(scope, receive, send)
        return
    await json_error(404, "Không có API này")(scope, receive, send)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    # uvicorn --reload chạy app ở process con: cấu hình log lại ở đây.
    configure_logging(settings)
    # Log truy cập của uvicorn trùng `http.request` (có request id, thời gian xử lý, người dùng)
    # và in cả /healthz: chỉ giữ cảnh báo.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with open_resources(settings, db_logs=False) as res:
            app.state.res = res
            if settings.auth_mode == "jwt" and settings.ui_password is not None:
                secret = await auth_tokens.resolve_secret(settings, res.redis)
                app.state.token_auth = auth_tokens.TokenAuth(
                    username=settings.ui_username,
                    password=settings.ui_password.get_secret_value(),
                    secret=secret,
                    hours=settings.auth_token_hours,
                )
            if settings.auth_mode == "proxy":
                log.info(
                    "ui.auth_proxy",
                    user_header=settings.auth_proxy_user_header,
                    email_header=settings.auth_proxy_email_header,
                    reason="người dùng lấy từ header của proxy SSO: API không được mở ra ngoài",
                )
            elif settings.auth_mode == "none":
                log.warning("ui.no_password", reason="chưa đặt UI_PASSWORD: không hỏi đăng nhập")
            # Event loop đứng quá WATCHDOG_SECONDS (mọi request treo) thì process thoát (mã 70) để
            # Docker dựng lại (`jobcrawler.watchdog`).
            dog = start_watchdog("api", max_stall=settings.watchdog_seconds)
            try:
                yield
            finally:
                dog.stop()

    app = FastAPI(
        title="jobcrawler",
        description="Giao diện vận hành và API của jobcrawler.",
        lifespan=lifespan,
        docs_url="/api/docs",
        # Bản commit ở backend/openapi.json (scripts/export_openapi.py), giao diện sinh kiểu từ đó.
        openapi_url="/api/openapi.json",
        redoc_url=None,
        generate_unique_id_function=operation_id,
    )
    app.state.settings = settings

    def redis() -> AuthRedis | None:
        res = getattr(app.state, "res", None)  # có sau khi lifespan mở tài nguyên
        return None if res is None else res.redis

    # Chặn đoán mật khẩu ở `POST /api/auth/login`; khoá ký JWT có sau khi lifespan đọc Redis.
    app.state.throttle = LoginThrottle(redis, prefix=settings.redis_prefix)
    app.state.token_auth = None

    def token_auth() -> auth_tokens.TokenAuth | None:
        return app.state.token_auth

    app.add_middleware(
        GuardMiddleware,
        username=settings.ui_username,
        token_auth=token_auth if settings.auth_mode == "jwt" else None,
        redis=redis,
        prefix=settings.redis_prefix,
        proxy_user_header=settings.auth_proxy_user_header,
        proxy_email_header=settings.auth_proxy_email_header,
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
    app.router.default = unknown_path
    return app


def openapi_document() -> str:
    """Spec như `/api/openapi.json`, dạng JSON khoá sắp xếp (diff gọn): bản commit ở
    backend/openapi.json (`scripts/export_openapi.py`). Không mở Postgres / Redis (lifespan không
    chạy), không đọc .env: spec chỉ phụ thuộc mã."""
    spec = create_app(Settings(_env_file=None)).openapi()  # pyright: ignore[reportCallIssue]
    return json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
