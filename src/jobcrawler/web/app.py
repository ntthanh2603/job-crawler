"""Ứng dụng web (FastAPI): API JSON + phục vụ giao diện React (frontend/, build vào `dist/`).

`jobcrawler ui` hoặc service `api` trong compose. Dùng chung `open_resources` và các hàm trong
`jobcrawler.services` như CLI; worker vẫn là process riêng, giao diện chỉ đưa job vào queue.
"""

from __future__ import annotations

import base64
import binascii
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
)
from starlette.types import ASGIApp, Receive, Scope, Send

from jobcrawler.config import Settings, get_settings
from jobcrawler.logging import configure_logging, get_logger
from jobcrawler.queues import make_queue
from jobcrawler.resources import open_resources
from jobcrawler.web import api

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


class GuardMiddleware:
    """Đăng nhập HTTP Basic (khi có UI_PASSWORD) và chặn request ghi từ site khác (CSRF: trình
    duyệt tự gửi kèm thông tin đăng nhập Basic)."""

    def __init__(self, app: ASGIApp, *, username: str, password: str | None) -> None:
        self.app = app
        self.username = username
        self.password = password

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
        user = self._user(headers)
        if user is None:
            response = PlainTextResponse(
                "Cần đăng nhập",
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="jobcrawler", charset="UTF-8"'},
            )
            await response(scope, receive, send)
            return
        if scope["method"] in _UNSAFE and not self._same_origin(headers):
            await PlainTextResponse("Request từ site khác bị chặn", status_code=403)(
                scope, receive, send
            )
            return
        scope.setdefault("state", {})["user"] = user
        await self.app(scope, receive, send)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    # uvicorn --reload chạy app ở process con: cấu hình log lại ở đây.
    configure_logging(settings)
    company_queue = make_queue(settings, settings.queue_name)
    board_queue = make_queue(settings, settings.board_queue_name)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await company_queue.connect()
        await board_queue.connect()
        try:
            async with open_resources(settings, queue=company_queue, db_logs=False) as res:
                app.state.res = res
                app.state.board_queue = board_queue
                if settings.ui_password is None:
                    log.warning(
                        "ui.no_password", reason="chưa đặt UI_PASSWORD: không hỏi đăng nhập"
                    )
                yield
        finally:
            await board_queue.disconnect()
            await company_queue.disconnect()

    app = FastAPI(
        title="jobcrawler",
        description="Giao diện vận hành và API của jobcrawler.",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    app.state.settings = settings
    password = settings.ui_password.get_secret_value() if settings.ui_password else None
    app.add_middleware(GuardMiddleware, username=settings.ui_username, password=password)

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
    dist = settings.ui_dist or DEFAULT_DIST
    if (dist / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

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
