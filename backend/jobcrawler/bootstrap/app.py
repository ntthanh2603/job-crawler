"""Ứng dụng web (FastAPI): API JSON (`/api/...`) và `/healthz`. Giao diện React là service riêng
(nginx phục vụ bản build, chỉ chuyển `/api/` tới đây).

`jobcrawler api` (trong compose: một process của container `backend`). Dùng chung
`open_resources` và các hàm trong `jobcrawler.services` như CLI; worker vẫn là process riêng, giao
diện chỉ đưa job vào queue.

Middleware, từ ngoài vào: `RequestIdMiddleware` (request id, log truy cập, lỗi chưa xử lý thành 500)
→ `GuardMiddleware` (đăng nhập: header của proxy SSO hoặc JWT do `POST /api/auth/login` cấp, chặn
CSRF; API tích hợp `/api/integration/v1` chỉ nhận khoá API) → FastAPI. Đoán mật khẩu bị chặn ở
chính endpoint đăng nhập (`LoginThrottle`). Mọi lỗi trả về cùng dạng JSON `{"detail": ...}` như
`HTTPException` của FastAPI.
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

from jobcrawler.common.config import Settings, get_settings
from jobcrawler.common.logging import configure_logging, get_logger
from jobcrawler.common.watchdog import start_watchdog
from jobcrawler.database.ids import uuid7
from jobcrawler.modules.api_clients import api_clients_router as api_clients_api
from jobcrawler.modules.audit import audit_router as audit_api
from jobcrawler.modules.audit import audit_service
from jobcrawler.modules.auth import auth_router as auth_api
from jobcrawler.modules.auth import auth_service
from jobcrawler.modules.auth.auth_service import TokenAuth
from jobcrawler.modules.boards import boards_router as boards_api
from jobcrawler.modules.companies import companies_router as companies_api
from jobcrawler.modules.crawl import crawl_router as crawl_api
from jobcrawler.modules.evaluate import evaluate_router as evaluate_api
from jobcrawler.modules.gateway import gateway_router as workers_api
from jobcrawler.modules.integration import integration_router as integration_api
from jobcrawler.modules.integration import key_service
from jobcrawler.modules.jobs import jobs_router as jobs_api
from jobcrawler.modules.live import live_router, live_service
from jobcrawler.modules.maintenance import maintenance_router as maintenance_api
from jobcrawler.modules.meta import meta_router as meta_api
from jobcrawler.modules.overview import overview_router as overview_api
from jobcrawler.modules.proxies import proxies_router as proxies_api
from jobcrawler.modules.queue import queue_router as queue_api
from jobcrawler.modules.runs import runs_router as runs_api
from jobcrawler.modules.settings import settings_router as settings_api
from jobcrawler.services.resources import open_resources

log = get_logger(__name__)

# API của từng module (`modules/*/*_router.py`). Spec ghi khoá sắp xếp nên thứ tự không đổi spec,
# chỉ đổi thứ tự khớp route: đường dẫn cố định (`/jobs/export`) đứng trước đường dẫn có tham số.
ROUTERS = (
    auth_api.router,
    meta_api.router,
    overview_api.router,
    maintenance_api.router,
    companies_api.router,
    runs_api.router,
    jobs_api.router,
    evaluate_api.router,
    boards_api.router,
    queue_api.router,
    crawl_api.router,
    audit_api.router,
    settings_api.router,
    proxies_api.router,
    workers_api.router,
    api_clients_api.router,
    integration_api.router,
    live_router.router,
)

# Nhóm endpoint ở `/api/docs`, theo thứ tự hiện: mỗi router một nhóm (`APIRouter(tags=[...])` ở
# `modules/*/*_router.py`); test kiểm mọi endpoint thuộc đúng một nhóm có ở đây.
OPENAPI_TAGS: list[dict[str, str]] = [
    {"name": "auth", "description": "Đăng nhập / đăng xuất (JWT), người dùng hiện tại"},
    {"name": "overview", "description": "Trang Tổng quan: số liệu, biểu đồ tính sẵn"},
    {
        "name": "companies",
        "description": "Doanh nghiệp: danh sách, chi tiết, thêm, nạp CSV, đưa vào hàng đợi",
    },
    {
        "name": "crawl",
        "description": "Thu thập: bộ khung + lịch của doanh nghiệp, build / poll ngay, độ trễ phát"
        " hiện",
    },
    {"name": "runs", "description": "Lượt build / poll: tóm tắt, request, log, trang parse lỗi"},
    {"name": "jobs", "description": "Việc làm: danh sách, xuất CSV, chi tiết, kiểm tay"},
    {"name": "eval", "description": "Đánh giá độ chính xác trên golden set"},
    {"name": "boards", "description": "Trang tuyển dụng: trạng thái index, chạy index ngay"},
    {"name": "queue", "description": "Hàng đợi build / poll / index"},
    {
        "name": "workers",
        "description": "Worker lấy trang: danh sách, khoá tham gia, dừng nhận việc",
    },
    {"name": "proxies", "description": "Proxy chung: trạng thái xoay vòng, kiểm tra qua worker"},
    {
        "name": "api-clients",
        "description": "Hệ thống ngoài dùng API tích hợp: tạo, khoá API, phạm vi đọc (trang Tích"
        " hợp)",
    },
    {
        "name": "integration",
        "description": "API cho hệ thống ngoài (khoá API `jca_…`): đẩy doanh nghiệp, việc làm, feed"
        " thay đổi",
    },
    {"name": "settings", "description": "Cài đặt sửa ở giao diện (đè biến môi trường / .env)"},
    {"name": "audit", "description": "Nhật ký thao tác"},
    {"name": "maintenance", "description": "Chạy bảo trì ngay"},
    {"name": "live", "description": "Cập nhật trực tiếp bằng Server-Sent Events"},
    {"name": "meta", "description": "Giá trị cho các bộ lọc của giao diện"},
]

# Không qua đăng nhập giao diện: healthcheck.
# Không qua đăng nhập: healthcheck, đăng nhập / đăng xuất (tự đọc token).
PUBLIC_PATHS = ("/healthz", "/api/auth/login", "/api/auth/logout")
# Giá trị header người dùng của proxy SSO dài hơn chừng này thì cắt (ghi vào log / nhật ký).
MAX_PROXY_USER_CHARS = 256
_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
BEARER_CHALLENGE = 'Bearer realm="jobcrawler"'
INTEGRATION_CHALLENGE = 'Bearer realm="jobcrawler-integration"'

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
    """Chặn đoán mật khẩu ở `POST /api/auth/login` theo IP, đếm trên Redis (dùng chung mọi process
    API).

    Khoá `<redis_prefix>:auth:fail:<ip>`: mỗi lần đăng nhập sai INCR rồi EXPIRE `window` giây; đủ
    `limit` lần thì mọi lần đăng nhập từ IP đó (kể cả mật khẩu đúng) nhận 429 kèm `Retry-After` tới
    khi khoá hết hạn; đăng nhập đúng thì xoá bộ đếm. Request khác (đã có JWT, hoặc chưa đăng nhập
    nhận 401) không qua đây, không bị tính.

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


INTEGRATION_PREFIX = "/api/integration/"


def _api_key(headers: Headers) -> str | None:
    """Khoá API trong `Authorization: Bearer jca_…` hoặc `X-API-Key` (không phải JWT)."""
    scheme, _, value = headers.get("authorization", "").partition(" ")
    if scheme.lower() == "bearer" and value.strip().startswith(key_service.KEY_PREFIX):
        return value.strip()
    value = headers.get("x-api-key", "").strip()
    return value or None


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
      `Authorization: Bearer` (`modules/auth/auth_service.py`): đúng chữ ký, còn hạn, chưa thu hồi.
      Thiếu / sai thì 401 JSON kèm `WWW-Authenticate: Bearer` (giao diện chuyển tới trang đăng
      nhập).
    - `none`: không hỏi đăng nhập, người dùng là `username`.

    Riêng `/api/integration/v1/*` (hệ thống ngoài, mọi chế độ): chỉ nhận khoá API
    (`Authorization: Bearer jca_…` hoặc `X-API-Key`, `modules/integration/key_service.py`); bỏ qua
    cookie / header SSO, không kiểm cùng site (không có cookie nên không bị CSRF). Khoá sai / thu
    hồi → 401, IP gửi khoá sai nhiều lần hoặc khoá vượt giới hạn request mỗi phút → 429. Khoá API
    gửi tới đường khác → 401. Client ở `request.state.api_client`, người dùng `api:<tên client>`.

    Người dùng đã đăng nhập ở `request.state.user` (và `actor` trong contextvars của structlog để
    log của request kèm theo; nhật ký thao tác ghi đúng người).
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        username: str,
        token_auth: Callable[[], TokenAuth | None] | None = None,
        api_keys: Callable[[], key_service.ApiKeyAuth | None] | None = None,
        redis: Callable[[], AuthRedis | None] | None = None,
        prefix: str = "jc",
        proxy_user_header: str | None = None,
        proxy_email_header: str | None = None,
    ) -> None:
        self.app = app
        self.username = username
        self.token_auth = token_auth
        self.api_keys = api_keys or (lambda: None)
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

    async def _token_claims(self, headers: Headers) -> auth_service.Claims | None:
        auth = self.token_auth() if self.token_auth else None
        token = auth_service.token_from(headers)
        if auth is None or token is None:
            return None
        claims = auth.verify(token)
        if claims is None:
            return None
        redis = self.redis()
        if redis is not None:
            try:
                if await auth_service.is_revoked(redis, self.prefix, claims.jti):
                    return None
            except Exception as exc:  # Redis lỗi: không khoá mọi người ở ngoài
                log.warning("auth.revoked_check_failed", error=str(exc))
        return claims

    @staticmethod
    def _same_origin(headers: Headers) -> bool:
        host = headers.get("host", "")
        source = headers.get("origin") or headers.get("referer")
        if not source:
            return True  # client không phải trình duyệt (curl, script): không bị CSRF
        return urlsplit(source).netloc == host

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        # Nguồn request cho nhật ký thao tác (cả đăng nhập / đăng xuất, đi tắt phía dưới).
        audit_service.bind_request(_client_ip(scope), headers.get("user-agent"))
        if scope["path"] in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return
        api_key = _api_key(headers)
        if scope["path"].startswith(INTEGRATION_PREFIX):
            await self._integration(scope, receive, send, api_key)
            return
        if api_key is not None:
            await json_error(401, "Khoá API chỉ dùng cho /api/integration/v1")(scope, receive, send)
            return
        if self.proxy_user_header is not None:
            user = self._proxy_user(headers)
            if user is None:
                await json_error(401, "Cần đăng nhập SSO")(scope, receive, send)
                return
        elif self.token_auth is not None:
            claims = await self._token_claims(headers)
            if claims is None:
                # `Bearer`: trình duyệt không bật hộp đăng nhập; giao diện nhận ra để chuyển tới
                # trang đăng nhập (khác 401 của SSO).
                challenge = {"WWW-Authenticate": BEARER_CHALLENGE}
                await json_error(401, "Cần đăng nhập", challenge)(scope, receive, send)
                return
            user = claims.user
            # Request dài (luồng SSE `modules/live/live_service.py`) tự xét lại hạn / thu hồi của
            # token này.
            scope.setdefault("state", {})["auth_claims"] = claims
        else:
            user = self.username
        await self._pass(scope, receive, send, headers, user)

    async def _integration(
        self, scope: Scope, receive: Receive, send: Send, api_key: str | None
    ) -> None:
        """Request của hệ thống ngoài: khoá API hợp lệ, chưa vượt giới hạn."""
        auth = self.api_keys()
        if auth is None:
            await json_error(503, "API tích hợp chưa sẵn sàng")(scope, receive, send)
            return
        ip = _client_ip(scope)
        if blocked := await auth.blocked(ip):
            retry = {"Retry-After": str(blocked)}
            await json_error(429, "Sai khoá API quá nhiều lần", retry)(scope, receive, send)
            return
        principal = await auth.verify(api_key) if api_key else None
        if principal is None:
            if api_key:
                await auth.failed(ip)
            challenge = {"WWW-Authenticate": INTEGRATION_CHALLENGE}
            await json_error(401, "Cần khoá API hợp lệ", challenge)(scope, receive, send)
            return
        if wait := await auth.rate_limited(principal.key_id):
            retry = {"Retry-After": str(wait)}
            await json_error(429, "Vượt giới hạn request mỗi phút", retry)(scope, receive, send)
            return
        state = scope.setdefault("state", {})
        state["api_client"] = principal
        state["user"] = principal.actor
        structlog.contextvars.bind_contextvars(actor=principal.actor)
        await self.app(scope, receive, send)

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
    FastAPI ghép thêm đường dẫn + method). Tên hàm trong các `modules/*/*_router.py` không được
    trùng nhau (test kiểm)."""
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
                secret = await auth_service.resolve_secret(settings, res.redis)
                app.state.token_auth = auth_service.TokenAuth(
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
            app.state.api_keys = key_service.ApiKeyAuth(res)
            # Cập nhật trực tiếp (`GET /api/events`): task đọc chung của từng chủ đề.
            app.state.live = live_service.LiveHub(live_service.resource_loader(res, settings))
            # Event loop đứng quá WATCHDOG_SECONDS (mọi request treo) thì process thoát (mã 70) để
            # Docker dựng lại (`jobcrawler.common.watchdog`).
            dog = start_watchdog("api", max_stall=settings.watchdog_seconds)
            try:
                yield
            finally:
                dog.stop()
                app.state.live.close()
                app.state.live = None

    app = FastAPI(
        title="jobcrawler",
        description="Giao diện vận hành và API của jobcrawler.",
        lifespan=lifespan,
        docs_url="/api/docs",
        # Bản commit ở backend/openapi.json (scripts/export_openapi.py), giao diện sinh kiểu từ đó.
        openapi_url="/api/openapi.json",
        redoc_url=None,
        generate_unique_id_function=operation_id,
        openapi_tags=OPENAPI_TAGS,
    )
    app.state.settings = settings

    def redis() -> AuthRedis | None:
        res = getattr(app.state, "res", None)  # có sau khi lifespan mở tài nguyên
        return None if res is None else res.redis

    # Chặn đoán mật khẩu ở `POST /api/auth/login`; khoá ký JWT có sau khi lifespan đọc Redis.
    app.state.throttle = LoginThrottle(redis, prefix=settings.redis_prefix)
    app.state.token_auth = None
    app.state.api_keys = None  # `key_service.ApiKeyAuth`, có sau khi lifespan mở tài nguyên
    app.state.live = None  # `live_service.LiveHub`, có sau khi lifespan mở tài nguyên

    def token_auth() -> auth_service.TokenAuth | None:
        return app.state.token_auth

    def api_keys() -> key_service.ApiKeyAuth | None:
        return app.state.api_keys

    app.add_middleware(
        GuardMiddleware,
        username=settings.ui_username,
        token_auth=token_auth if settings.auth_mode == "jwt" else None,
        api_keys=api_keys,
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

    for router in ROUTERS:
        app.include_router(router)
    app.router.default = unknown_path
    return app


def openapi_document() -> str:
    """Spec như `/api/openapi.json`, dạng JSON khoá sắp xếp (diff gọn): bản commit ở
    backend/openapi.json (`scripts/export_openapi.py`). Không mở Postgres / Redis (lifespan không
    chạy), không đọc .env: spec chỉ phụ thuộc mã."""
    spec = create_app(Settings(_env_file=None)).openapi()  # pyright: ignore[reportCallIssue]
    return json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
