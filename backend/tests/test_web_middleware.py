"""Middleware của web, không cần DB: request id + log truy cập, lỗi dạng JSON, chặn đoán mật khẩu
HTTP Basic, cache file tĩnh có hash."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import structlog
from fastapi import FastAPI, Request
from pydantic import SecretStr
from structlog.testing import LogCapture
from structlog.typing import EventDict

from jobcrawler.config import Settings
from jobcrawler.web import app as web_app
from jobcrawler.web.app import (
    LOGIN_FAIL_LIMIT,
    LOGIN_FAIL_WINDOW,
    GuardMiddleware,
    LoginThrottle,
    RequestIdMiddleware,
    create_app,
)

PREFIX = "jc-test"
Logs = list[EventDict]


class FakeRedis:
    """Đủ lệnh cho `LoginThrottle`; `broken` → mọi lệnh lỗi như Redis mất kết nối."""

    def __init__(self, *, broken: bool = False) -> None:
        self.values: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.broken = broken

    def _check(self) -> None:
        if self.broken:
            raise ConnectionError("redis down")

    async def get(self, name: str) -> bytes | None:
        self._check()
        value = self.values.get(name)
        return None if value is None else str(value).encode()

    async def incr(self, name: str) -> int:
        self._check()
        self.values[name] = self.values.get(name, 0) + 1
        return self.values[name]

    async def expire(self, name: str, time: int) -> bool:
        self._check()
        if name not in self.values:
            return False
        self.ttls[name] = time
        return True

    async def ttl(self, name: str) -> int:
        self._check()
        if name not in self.values:
            return -2
        return self.ttls.get(name, -1)

    async def delete(self, *names: str) -> int:
        self._check()
        removed = 0
        for name in names:
            removed += self.values.pop(name, None) is not None
            self.ttls.pop(name, None)
        return removed


@pytest.fixture(autouse=True)
def _clean_contextvars() -> Iterator[None]:
    yield
    structlog.contextvars.clear_contextvars()


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch) -> Logs:
    """Event log của `jobcrawler.web.app` (kèm contextvars như cấu hình thật)."""
    capture = LogCapture()
    logger = structlog.wrap_logger(
        None,
        processors=[structlog.contextvars.merge_contextvars, capture],
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    monkeypatch.setattr(web_app, "log", logger)
    return capture.entries


def basic(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


GOOD = basic("admin", "pw")
BAD = basic("admin", "sai")


def small_app(
    *, password: str | None = "pw", redis: FakeRedis | None = None, ip: str = "10.0.0.1"
) -> httpx.AsyncClient:
    """App FastAPI với middleware xếp như `create_app` (lỗi route đi qua cả hai middleware)."""
    app = FastAPI()

    @app.api_route("/api/ctx", methods=["GET", "POST"])
    async def ctx(request: Request) -> dict[str, Any]:
        bound = structlog.contextvars.get_contextvars()
        return {
            "request_id": bound.get("request_id"),
            "actor": bound.get("actor"),
            "state_request_id": request.state.request_id,
            "user": getattr(request.state, "user", None),
        }

    @app.get("/api/boom")
    async def boom() -> None:
        raise RuntimeError("hỏng")

    @app.get("/healthz")
    async def healthz() -> str:
        return "ok"

    @app.get("/page")
    async def page() -> str:
        return "page"

    throttle = LoginThrottle(lambda: redis, prefix=PREFIX)
    app.add_middleware(GuardMiddleware, username="admin", password=password, throttle=throttle)
    app.add_middleware(RequestIdMiddleware)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(ip, 123)), base_url="http://ui.test"
    )


def events(logs: Logs, name: str) -> Logs:
    return [e for e in logs if e["event"] == name]


# --- request id + log truy cập ------------------------------------------------------------------


async def test_request_id_is_generated_bound_and_logged(logs: Logs) -> None:
    async with small_app() as client:
        r = await client.get("/api/ctx", params={"q": "x"}, headers=GOOD)
    assert r.status_code == 200
    request_id = r.headers["x-request-id"]
    assert UUID(request_id).version == 7
    # Hợp đồng: trong lúc xử lý request, request id có trong contextvars của structlog.
    assert r.json() == {
        "request_id": request_id,
        "actor": "admin",
        "state_request_id": request_id,
        "user": "admin",
    }
    [entry] = events(logs, "http.request")
    assert entry["request_id"] == request_id
    assert (entry["method"], entry["path"], entry["status"], entry["actor"]) == (
        "GET",
        "/api/ctx",
        200,
        "admin",
    )
    assert entry["log_level"] == "info"
    assert entry["duration_ms"] >= 0


@pytest.mark.parametrize(
    ("incoming", "kept"),
    [
        ("0192F0C1-8A2B-7C3D-8E4F-0123456789AB", "0192f0c1-8a2b-7c3d-8e4f-0123456789ab"),
        (str(uuid4()), None),
        ("abc", False),
        ("1; DROP TABLE logs", False),
        ("0" * 1000, False),
    ],
)
async def test_incoming_request_id_only_if_uuid(incoming: str, kept: str | bool | None) -> None:
    async with small_app() as client:
        r = await client.get("/api/ctx", headers={**GOOD, "X-Request-Id": incoming})
    returned = r.headers["x-request-id"]
    assert r.json()["request_id"] == returned
    if kept is False:
        assert returned != incoming
        assert UUID(returned).version == 7
    else:
        assert returned == (kept or incoming)


async def test_each_request_starts_with_clean_contextvars() -> None:
    structlog.contextvars.bind_contextvars(actor="cũ", run_id="x")
    async with small_app(password=None) as client:
        first = (await client.get("/api/ctx")).json()
        second = (await client.get("/api/ctx")).json()
    assert first["request_id"] != second["request_id"]
    assert first["actor"] == "admin"  # không có mật khẩu: người dùng mặc định
    assert "run_id" not in structlog.contextvars.get_contextvars()


async def test_static_and_health_are_not_access_logged(logs: Logs) -> None:
    async with small_app() as client:
        health = await client.get("/healthz")
        page = await client.get("/page", headers=GOOD)
    assert health.status_code == page.status_code == 200
    assert "x-request-id" in health.headers
    assert "x-request-id" in page.headers
    assert events(logs, "http.request") == []


# --- lỗi dạng JSON -----------------------------------------------------------------------------


async def test_auth_errors_are_json(logs: Logs) -> None:
    async with small_app() as client:
        missing = await client.get("/api/ctx")
        evil = await client.post("/api/ctx", headers={**GOOD, "Origin": "https://evil.example"})
    assert missing.status_code == 401
    assert missing.json() == {"detail": "Cần đăng nhập"}
    assert missing.headers["www-authenticate"].startswith("Basic")
    assert "x-request-id" in missing.headers
    assert evil.status_code == 403
    assert evil.json() == {"detail": "Request từ site khác bị chặn"}
    statuses = [(e["status"], e["actor"]) for e in events(logs, "http.request")]
    assert statuses == [(401, None), (403, None)]


async def test_unhandled_error_is_logged_and_returns_json_500(logs: Logs) -> None:
    async with small_app() as client:
        r = await client.get("/api/boom", headers=GOOD)
    assert r.status_code == 500
    request_id = r.headers["x-request-id"]
    assert r.json() == {"detail": "Lỗi máy chủ", "request_id": request_id}
    [error] = events(logs, "http.unhandled")
    assert error["exc_info"] is True
    assert error["log_level"] == "error"
    assert (error["request_id"], error["actor"], error["path"]) == (
        request_id,
        "admin",
        "/api/boom",
    )
    [access] = events(logs, "http.request")
    assert access["status"] == 500


# --- chặn đoán mật khẩu ------------------------------------------------------------------------


async def test_login_throttle_locks_ip_after_limit(logs: Logs) -> None:
    redis = FakeRedis()
    key = f"{PREFIX}:auth:fail:10.0.0.1"
    async with small_app(redis=redis) as client:
        for _ in range(LOGIN_FAIL_LIMIT):
            assert (await client.get("/api/ctx", headers=BAD)).status_code == 401
        assert redis.values[key] == LOGIN_FAIL_LIMIT
        assert redis.ttls[key] == LOGIN_FAIL_WINDOW
        for headers in (BAD, GOOD):  # mật khẩu đúng cũng bị chặn: không đoán tiếp được
            r = await client.get("/api/ctx", headers=headers)
            assert r.status_code == 429
            assert r.json() == {"detail": "Đăng nhập sai quá nhiều lần, thử lại sau"}
            assert r.headers["retry-after"] == str(LOGIN_FAIL_WINDOW)
        assert redis.values[key] == LOGIN_FAIL_LIMIT  # request bị chặn không tính thêm
    [locked] = events(logs, "auth.locked")
    assert (locked["ip"], locked["failures"]) == ("10.0.0.1", LOGIN_FAIL_LIMIT)
    # IP khác không bị ảnh hưởng.
    async with small_app(redis=redis, ip="10.0.0.2") as other:
        assert (await other.get("/api/ctx", headers=GOOD)).status_code == 200


async def test_login_throttle_counts_only_wrong_credentials_and_clears_on_success() -> None:
    redis = FakeRedis()
    key = f"{PREFIX}:auth:fail:10.0.0.1"
    async with small_app(redis=redis) as client:
        await client.get("/api/ctx")  # chưa gửi mật khẩu (trình duyệt sắp hỏi): không tính
        await client.get("/api/ctx", headers={"Authorization": ""})
        assert key not in redis.values
        for _ in range(3):
            await client.get("/api/ctx", headers=BAD)
        assert redis.values[key] == 3
        assert (await client.get("/api/ctx", headers=GOOD)).status_code == 200
        assert key not in redis.values
        assert (await client.get("/healthz")).status_code == 200


async def test_login_throttle_restores_lost_ttl() -> None:
    redis = FakeRedis()
    key = f"{PREFIX}:auth:fail:10.0.0.1"
    redis.values[key] = LOGIN_FAIL_LIMIT  # khoá không còn TTL (EXPIRE lỗi giữa chừng)
    async with small_app(redis=redis) as client:
        r = await client.get("/api/ctx", headers=GOOD)
    assert r.status_code == 429
    assert redis.ttls[key] == LOGIN_FAIL_WINDOW


async def test_login_throttle_fails_open_when_redis_is_down(logs: Logs) -> None:
    async with small_app(redis=FakeRedis(broken=True)) as client:
        assert (await client.get("/api/ctx", headers=BAD)).status_code == 401
        assert (await client.get("/api/ctx", headers=GOOD)).status_code == 200
    warnings = events(logs, "auth.throttle_unavailable")
    assert warnings
    assert all(w["log_level"] == "warning" for w in warnings)


async def test_no_password_means_no_throttle() -> None:
    redis = FakeRedis()
    async with small_app(password=None, redis=redis) as client:
        assert (await client.get("/api/ctx", headers=BAD)).status_code == 200
    assert redis.values == {}


# --- create_app: lắp middleware, file tĩnh -----------------------------------------------------


def full_app(tmp_path: Path, redis: FakeRedis | None = None) -> tuple[FastAPI, httpx.AsyncClient]:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<div id=root></div>", encoding="utf-8")
    (dist / "assets" / "app-1a2b3c.js").write_text("console.log(1)", encoding="utf-8")
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        ui_dist=dist,
        ui_password=SecretStr("pw"),
        redis_prefix="jc-app",
    )
    app = create_app(settings)
    if redis is not None:  # không chạy lifespan (cần DB): gắn Redis giả như `open_resources`
        app.state.res = SimpleNamespace(redis=redis)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://ui.test")
    return app, client


@pytest.mark.usefixtures("clean_env")
async def test_create_app_wires_request_id_json_errors_and_throttle(tmp_path: Path) -> None:
    redis = FakeRedis()
    app, client = full_app(tmp_path, redis)

    async def boom() -> None:
        raise RuntimeError("hỏng")

    app.add_api_route("/api/boom", boom)
    app.router.routes.insert(0, app.router.routes.pop())  # trước route SPA bắt mọi đường dẫn
    async with client:
        health = await client.get("/healthz")
        assert health.text == "ok"
        assert UUID(health.headers["x-request-id"])
        unauthorized = await client.get("/api/me", headers=BAD)
        assert unauthorized.status_code == 401
        assert unauthorized.json() == {"detail": "Cần đăng nhập"}
        assert redis.values == {"jc-app:auth:fail:127.0.0.1": 1}
        missing = await client.get("/api/khong-co", headers=GOOD)
        assert missing.status_code == 404
        assert missing.json() == {"detail": "Không có API này"}
        assert redis.values == {}  # đăng nhập đúng: xoá bộ đếm
        error = await client.get("/api/boom", headers=GOOD)
        assert error.status_code == 500
        assert error.json() == {
            "detail": "Lỗi máy chủ",
            "request_id": error.headers["x-request-id"],
        }


@pytest.mark.usefixtures("clean_env")
async def test_hashed_assets_are_cached_long_but_index_is_not(tmp_path: Path) -> None:
    _, client = full_app(tmp_path)  # không có Redis (lifespan chưa chạy): không chặn, vẫn chạy
    async with client:
        asset = await client.get("/assets/app-1a2b3c.js", headers=GOOD)
        assert asset.text == "console.log(1)"
        assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
        etag = asset.headers["etag"]
        cached = await client.get("/assets/app-1a2b3c.js", headers={**GOOD, "If-None-Match": etag})
        assert cached.status_code == 304
        assert cached.headers["cache-control"] == "public, max-age=31536000, immutable"
        gone = await client.get("/assets/khong-co.js", headers=GOOD)
        assert gone.status_code == 404
        assert "immutable" not in gone.headers.get("cache-control", "")
        index = await client.get("/jobs/1", headers=GOOD)
        assert index.headers["cache-control"] == "no-cache"
