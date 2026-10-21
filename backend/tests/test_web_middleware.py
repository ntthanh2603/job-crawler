"""Middleware của web, không cần DB: request id + log truy cập, lỗi dạng JSON, đăng nhập JWT (token,
đăng nhập / đăng xuất, chặn đoán mật khẩu), SSO qua header."""

from __future__ import annotations

import time
from collections.abc import Iterator
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

from jobcrawler.bootstrap import app as web_app
from jobcrawler.bootstrap.app import (
    LOGIN_FAIL_LIMIT,
    LOGIN_FAIL_WINDOW,
    GuardMiddleware,
    RequestIdMiddleware,
    create_app,
)
from jobcrawler.common.config import Settings
from jobcrawler.modules.auth import auth_router as auth_api
from jobcrawler.modules.auth.auth_service import COOKIE, TokenAuth

PREFIX = "jc-test"
Logs = list[EventDict]


class FakeRedis:
    """Đủ lệnh cho `LoginThrottle`; `broken` → mọi lệnh lỗi như Redis mất kết nối."""

    def __init__(self, *, broken: bool = False) -> None:
        self.values: dict[str, Any] = {}
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

    async def set(self, name: str, value: Any, *, ex: int | None = None, nx: bool = False) -> Any:
        self._check()
        if nx and name in self.values:
            return None
        self.values[name] = value
        if ex is not None:
            self.ttls[name] = ex
        return True

    async def exists(self, *names: str) -> int:
        self._check()
        return sum(name in self.values for name in names)

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
    """Event log của `jobcrawler.bootstrap.app` (kèm contextvars như cấu hình thật)."""
    capture = LogCapture()
    logger = structlog.wrap_logger(
        None,
        processors=[structlog.contextvars.merge_contextvars, capture],
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    monkeypatch.setattr(web_app, "log", logger)
    monkeypatch.setattr(auth_api, "log", logger)
    return capture.entries


AUTH = TokenAuth(username="admin", password="pw", secret=b"k" * 32, hours=1)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


GOOD = bearer(AUTH.issue().token)
BAD = bearer("khong-phai-jwt")


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

    app.add_middleware(
        GuardMiddleware,
        username="admin",
        token_auth=(lambda: AUTH) if password is not None else None,
        redis=lambda: redis,
        prefix=PREFIX,
    )
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
    # Bearer: trình duyệt không bật hộp đăng nhập; giao diện chuyển tới trang đăng nhập.
    assert missing.headers["www-authenticate"] == 'Bearer realm="jobcrawler"'
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


# --- JWT ---------------------------------------------------------------------------------------


def test_token_auth_issue_verify_and_invalidation() -> None:
    issued = AUTH.issue()
    claims = AUTH.verify(issued.token)
    assert claims is not None
    assert (claims.user, claims.jti, claims.expires_at) == ("admin", issued.jti, issued.expires_at)
    assert AUTH.verify(issued.token + "x") is None  # sai chữ ký
    expired = AUTH.issue(now=time.time() - 2 * AUTH.ttl)
    assert AUTH.verify(expired.token) is None
    # Đổi tài khoản / mật khẩu / khoá ký trong .env: token cũ hết dùng.
    for other in (
        TokenAuth(username="root", password="pw", secret=b"k" * 32, hours=1),
        TokenAuth(username="admin", password="moi", secret=b"k" * 32, hours=1),
        TokenAuth(username="admin", password="pw", secret=b"x" * 32, hours=1),
    ):
        assert other.verify(issued.token) is None
    assert AUTH.check_password("admin", "pw")
    assert not AUTH.check_password("admin", "sai")
    assert not AUTH.check_password("Admin", "pw")


async def test_token_from_cookie_or_bearer_and_revocation() -> None:
    redis = FakeRedis()
    issued = AUTH.issue()
    async with small_app(redis=redis) as client:
        by_header = await client.get("/api/ctx", headers=bearer(issued.token))
        by_cookie = await client.get(
            "/api/ctx", headers={"Cookie": f"a=b; {COOKIE}={issued.token}"}
        )
        assert by_header.json()["user"] == by_cookie.json()["user"] == "admin"
        redis.values[f"{PREFIX}:auth:revoked:{issued.jti}"] = 1
        assert (await client.get("/api/ctx", headers=bearer(issued.token))).status_code == 401
        assert (await client.get("/api/ctx", headers=BAD)).status_code == 401
        assert (await client.get("/healthz")).status_code == 200


async def test_revocation_check_fails_open_when_redis_is_down(logs: Logs) -> None:
    async with small_app(redis=FakeRedis(broken=True)) as client:
        assert (await client.get("/api/ctx", headers=GOOD)).status_code == 200
    assert events(logs, "auth.revoked_check_failed")


async def test_no_password_means_no_login() -> None:
    async with small_app(password=None) as client:
        assert (await client.get("/api/ctx", headers=BAD)).json()["user"] == "admin"


# --- create_app: đăng nhập, chặn đoán mật khẩu, đăng xuất ---------------------------------------


def full_app(
    redis: FakeRedis | None = None, *, ip: str = "127.0.0.1"
) -> tuple[FastAPI, httpx.AsyncClient]:
    settings = Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        ui_password=SecretStr("pw"),
        redis_prefix="jc-app",
    )
    app = create_app(settings)
    # Không chạy lifespan (cần DB): gắn Redis giả như `open_resources` và khoá ký như lifespan.
    app.state.res = SimpleNamespace(redis=redis or FakeRedis())
    app.state.token_auth = TokenAuth(username="admin", password="pw", secret=b"s" * 32, hours=1)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(ip, 1)), base_url="http://ui.test"
    )
    return app, client


LOGIN = "/api/auth/login"
WRONG = {"username": "admin", "password": "sai"}
RIGHT = {"username": "admin", "password": "pw"}


@pytest.mark.usefixtures("clean_env")
async def test_login_sets_cookie_and_token_then_logout_revokes(logs: Logs) -> None:
    _, client = full_app()
    async with client:
        assert (await client.get("/api/me")).status_code == 401
        r = await client.post(LOGIN, json=RIGHT)
        assert r.status_code == 200, r.text
        body = r.json()
        assert (body["user"], body["token_type"]) == ("admin", "bearer")
        cookie = r.headers["set-cookie"]
        assert cookie.startswith(f"{COOKIE}=")
        assert "httponly" in cookie.lower()
        assert "samesite=lax" in cookie.lower()
        # Cookie client giữ lại: gọi API được; script dùng Bearer cũng được.
        assert (await client.get("/api/me")).json()["user"] == "admin"
        token = body["access_token"]
        assert (await client.get("/api/me", headers=bearer(token))).status_code == 200

        out = await client.post("/api/auth/logout")
        assert out.json() == {"ok": True}
        assert f"{COOKIE}=" in out.headers["set-cookie"]  # xoá cookie
        # Token đã thu hồi: kể cả gửi lại bằng header cũng bị từ chối.
        assert (await client.get("/api/me", headers=bearer(token))).status_code == 401
    assert [e["event"] for e in logs if e["event"].startswith("auth.")] == [
        "auth.login",
        "auth.logout",
    ]


@pytest.mark.usefixtures("clean_env")
async def test_login_throttle_locks_ip_after_limit(logs: Logs) -> None:
    redis = FakeRedis()
    key = "jc-app:auth:fail:10.0.0.1"
    _, client = full_app(redis, ip="10.0.0.1")
    async with client:
        for _ in range(3):
            assert (await client.post(LOGIN, json=WRONG)).status_code == 401
        assert redis.values[key] == 3
        assert (await client.post(LOGIN, json=RIGHT)).status_code == 200
        assert key not in redis.values  # đăng nhập đúng: xoá bộ đếm
        for _ in range(LOGIN_FAIL_LIMIT):
            r = await client.post(LOGIN, json=WRONG)
            assert r.json() == {"detail": "Sai tên đăng nhập hoặc mật khẩu"}
        assert redis.ttls[key] == LOGIN_FAIL_WINDOW
        for body in (WRONG, RIGHT):  # mật khẩu đúng cũng bị chặn: không đoán tiếp được
            r = await client.post(LOGIN, json=body)
            assert r.status_code == 429
            assert r.headers["retry-after"] == str(LOGIN_FAIL_WINDOW)
    [locked] = events(logs, "auth.locked")
    assert (locked["ip"], locked["failures"]) == ("10.0.0.1", LOGIN_FAIL_LIMIT)
    _, other = full_app(redis, ip="10.0.0.2")  # IP khác không bị ảnh hưởng
    async with other:
        assert (await other.post(LOGIN, json=RIGHT)).status_code == 200


@pytest.mark.usefixtures("clean_env")
async def test_login_throttle_fails_open_when_redis_is_down(logs: Logs) -> None:
    _, client = full_app(FakeRedis(broken=True))
    async with client:
        assert (await client.post(LOGIN, json=WRONG)).status_code == 401
        assert (await client.post(LOGIN, json=RIGHT)).status_code == 200
    assert events(logs, "auth.throttle_unavailable")


@pytest.mark.usefixtures("clean_env")
async def test_login_restores_lost_ttl() -> None:
    redis = FakeRedis()
    key = "jc-app:auth:fail:127.0.0.1"
    redis.values[key] = LOGIN_FAIL_LIMIT  # khoá không còn TTL (EXPIRE lỗi giữa chừng)
    _, client = full_app(redis)
    async with client:
        r = await client.post(LOGIN, json=RIGHT)
    assert r.status_code == 429
    assert redis.ttls[key] == LOGIN_FAIL_WINDOW


@pytest.mark.usefixtures("clean_env")
async def test_login_without_password_mode_is_rejected() -> None:
    app = create_app(Settings(_env_file=None))  # pyright: ignore[reportCallIssue]
    app.state.res = SimpleNamespace(redis=FakeRedis())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ui.test") as client:
        r = await client.post(LOGIN, json={"username": "admin", "password": "x"})
    assert r.status_code == 400


@pytest.mark.usefixtures("clean_env")
async def test_create_app_wires_request_id_json_errors() -> None:
    app, client = full_app()

    async def boom() -> None:
        raise RuntimeError("hỏng")

    app.add_api_route("/api/boom", boom)
    good = bearer(app.state.token_auth.issue().token)
    async with client:
        health = await client.get("/healthz")
        assert health.text == "ok"
        assert UUID(health.headers["x-request-id"])
        unauthorized = await client.get("/api/me", headers=BAD)
        assert unauthorized.status_code == 401
        assert unauthorized.json() == {"detail": "Cần đăng nhập"}
        missing = await client.get("/api/khong-co", headers=good)
        assert missing.status_code == 404
        assert missing.json() == {"detail": "Không có API này"}
        error = await client.get("/api/boom", headers=good)
        assert error.status_code == 500
        assert error.json() == {
            "detail": "Lỗi máy chủ",
            "request_id": error.headers["x-request-id"],
        }


# --- SSO qua header của proxy tin cậy (oauth2-proxy) ---------------------------------------------

USER_HEADER = "X-Auth-Request-User"
EMAIL_HEADER = "X-Auth-Request-Email"


def proxy_app(redis: FakeRedis | None = None, *, email: bool = True) -> httpx.AsyncClient:
    """Như `small_app` nhưng ở chế độ proxy (có cả UI_PASSWORD: header SSO phải thắng)."""
    app = FastAPI()

    @app.api_route("/api/ctx", methods=["GET", "POST"])
    async def ctx(request: Request) -> dict[str, Any]:
        return {
            "actor": structlog.contextvars.get_contextvars().get("actor"),
            "user": getattr(request.state, "user", None),
        }

    @app.get("/healthz")
    async def healthz() -> str:
        return "ok"

    app.add_middleware(
        GuardMiddleware,
        username="admin",
        token_auth=lambda: AUTH,
        redis=lambda: redis,
        prefix=PREFIX,
        proxy_user_header=f" {USER_HEADER} ",
        proxy_email_header=EMAIL_HEADER if email else None,
    )
    app.add_middleware(RequestIdMiddleware)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("10.0.0.9", 1)), base_url="http://ui.test"
    )


async def test_proxy_header_sets_user_and_audit_actor(logs: Logs) -> None:
    async with proxy_app() as client:
        r = await client.get("/api/ctx", headers={USER_HEADER: "alice"})
        by_email = await client.get("/api/ctx", headers={EMAIL_HEADER: "bob@oasm.dev"})
        both = await client.get(
            "/api/ctx", headers={USER_HEADER: "carol", EMAIL_HEADER: "carol@oasm.dev"}
        )
        long = await client.get("/api/ctx", headers={USER_HEADER: "x" * 1000})
    assert r.json() == {"actor": "alice", "user": "alice"}
    assert by_email.json()["user"] == "bob@oasm.dev"  # thiếu header người dùng: dùng email
    assert both.json()["user"] == "carol"
    assert len(long.json()["user"]) == web_app.MAX_PROXY_USER_CHARS
    actors = [e["actor"] for e in events(logs, "http.request")]
    assert actors[:3] == ["alice", "bob@oasm.dev", "carol"]


async def test_proxy_mode_rejects_missing_header() -> None:
    async with proxy_app(FakeRedis(), email=False) as client:
        missing = await client.get("/api/ctx")
        blank = await client.get("/api/ctx", headers={USER_HEADER: "  "})
        # JWT không dùng được ở chế độ proxy, kể cả token hợp lệ.
        token_only = await client.get("/api/ctx", headers=GOOD)
        after = await client.get("/api/ctx", headers={USER_HEADER: "alice"})
        health = await client.get("/healthz")
    for r in (missing, blank, token_only):
        assert r.status_code == 401
        assert r.json() == {"detail": "Cần đăng nhập SSO"}
        assert "www-authenticate" not in r.headers
    assert after.status_code == 200
    assert health.status_code == 200


async def test_proxy_mode_still_blocks_cross_site_writes() -> None:
    async with proxy_app() as client:
        evil = await client.post(
            "/api/ctx", headers={USER_HEADER: "alice", "Origin": "https://evil.example"}
        )
        same = await client.post(
            "/api/ctx", headers={USER_HEADER: "alice", "Origin": "http://ui.test"}
        )
    assert evil.status_code == 403
    assert same.status_code == 200


@pytest.mark.usefixtures("clean_env")
@pytest.mark.parametrize(
    ("overrides", "headers", "expected"),
    [
        (
            {"auth_proxy_user_header": USER_HEADER, "auth_logout_url": "/oauth2/sign_out"},
            {USER_HEADER: "alice"},
            {"user": "alice", "auth": "proxy", "logout_url": "/oauth2/sign_out"},
        ),
        (
            {"ui_password": SecretStr("pw")},
            GOOD,
            {"user": "admin", "auth": "jwt", "logout_url": None},
        ),
        ({}, {}, {"user": "admin", "auth": "none", "logout_url": None}),
    ],
)
async def test_me_reports_auth_mode_and_logout_url(
    overrides: dict[str, Any], headers: dict[str, str], expected: dict[str, Any]
) -> None:
    settings = Settings(_env_file=None, **overrides)  # pyright: ignore[reportCallIssue]
    app = create_app(settings)
    app.state.res = SimpleNamespace(redis=FakeRedis())
    app.state.token_auth = AUTH if settings.auth_mode == "jwt" else None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ui.test"
    ) as client:
        r = await client.get("/api/me", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == expected
