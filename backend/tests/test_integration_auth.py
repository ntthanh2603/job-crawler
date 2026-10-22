"""Xác thực khoá API của hệ thống ngoài (`/api/integration/v1`, `key_service.ApiKeyAuth` +
`GuardMiddleware`), không cần DB: tra khoá giả, Redis giả."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, Request

from jobcrawler.bootstrap.app import GuardMiddleware, RequestIdMiddleware
from jobcrawler.common.config import Settings
from jobcrawler.modules.integration import key_service
from jobcrawler.modules.integration.key_service import ApiKeyAuth, Principal

from .test_web_middleware import AUTH, FakeRedis, bearer

KEY = key_service.new_key()
OTHER = key_service.new_key()
PRINCIPAL = Principal(
    key_id=uuid4(),
    client_id=uuid4(),
    client_name="đối tác",
    scopes=frozenset(key_service.SCOPES),
    all_companies=False,
    include_description=True,
)


class FakeSessions:
    """`sessionmaker()` / `.begin()` giả: `_touch` ghi `last_used_at` vào đây."""

    def __init__(self) -> None:
        self.touches = 0

    @contextlib.asynccontextmanager
    async def _session(self) -> AsyncIterator[Any]:
        sessions = self

        class Session:
            async def execute(self, *_: Any) -> None:
                sessions.touches += 1

        yield Session()

    def __call__(self) -> Any:
        return self._session()

    def begin(self) -> Any:
        return self._session()


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch) -> dict[str, Principal]:
    valid = {KEY: PRINCIPAL}

    async def lookup(_session: Any, key: str) -> Principal | None:
        return valid.get(key)

    monkeypatch.setattr(key_service, "lookup", lookup)
    return valid


def make_auth(*, rate: int = 600) -> ApiKeyAuth:
    settings = Settings(_env_file=None, integration_rate_per_minute=rate)  # pyright: ignore[reportCallIssue]
    res: Any = SimpleNamespace(settings=settings, redis=FakeRedis(), sessionmaker=FakeSessions())
    return ApiKeyAuth(res)


def client(auth: ApiKeyAuth | None, *, password: str | None = "pw") -> httpx.AsyncClient:
    app = FastAPI()

    @app.api_route("/api/integration/v1/x", methods=["GET", "POST"])
    async def x(request: Request) -> dict[str, Any]:
        principal = request.state.api_client
        return {"user": request.state.user, "client": principal.client_name}

    @app.get("/api/ctx")
    async def ctx(request: Request) -> dict[str, Any]:
        return {"user": request.state.user}

    app.add_middleware(
        GuardMiddleware,
        username="admin",
        token_auth=(lambda: AUTH) if password is not None else None,
        api_keys=lambda: auth,
        redis=lambda: None,
    )
    app.add_middleware(RequestIdMiddleware)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("10.0.0.9", 1)), base_url="http://ui.test"
    )


async def test_valid_key_reaches_integration_routes(keys: dict[str, Principal]) -> None:
    async with client(make_auth()) as c:
        r = await c.get("/api/integration/v1/x", headers=bearer(KEY))
        assert r.status_code == 200
        assert r.json() == {"user": "api:đối tác", "client": "đối tác"}
        # Header riêng cũng được; request ghi từ "site khác" không bị chặn (không có cookie).
        r = await c.post(
            "/api/integration/v1/x",
            headers={"X-API-Key": KEY, "Origin": "https://evil.test"},
        )
        assert r.status_code == 200


@pytest.mark.parametrize("password", ["pw", None])
async def test_integration_routes_need_a_key_in_every_auth_mode(
    keys: dict[str, Principal], password: str | None
) -> None:
    async with client(make_auth(), password=password) as c:
        missing = await c.get("/api/integration/v1/x")
        jwt = await c.get("/api/integration/v1/x", headers=bearer(AUTH.issue().token))
        wrong = await c.get("/api/integration/v1/x", headers=bearer(OTHER))
    for r in (missing, jwt, wrong):
        assert r.status_code == 401
        assert r.headers["www-authenticate"] == 'Bearer realm="jobcrawler-integration"'


async def test_api_key_is_refused_outside_integration_routes(keys: dict[str, Principal]) -> None:
    async with client(make_auth(), password=None) as c:
        r = await c.get("/api/ctx", headers=bearer(KEY))
    assert r.status_code == 401


async def test_not_ready_before_lifespan() -> None:
    async with client(None) as c:
        r = await c.get("/api/integration/v1/x", headers=bearer(KEY))
    assert r.status_code == 503


async def test_rate_limit_per_key(keys: dict[str, Principal]) -> None:
    async with client(make_auth(rate=2)) as c:
        codes = [(await c.get("/api/integration/v1/x", headers=bearer(KEY))) for _ in range(3)]
    assert [r.status_code for r in codes] == [200, 200, 429]
    assert 1 <= int(codes[-1].headers["retry-after"]) <= 60


async def test_ip_sending_wrong_keys_is_blocked(keys: dict[str, Principal]) -> None:
    auth = make_auth()
    async with client(auth) as c:
        for _ in range(key_service.FAIL_LIMIT):
            assert (await c.get("/api/integration/v1/x", headers=bearer(OTHER))).status_code == 401
        blocked = await c.get("/api/integration/v1/x", headers=bearer(KEY))
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"] == str(key_service.FAIL_WINDOW)


async def test_revoked_key_stops_after_cache(keys: dict[str, Principal]) -> None:
    auth = make_auth()
    async with client(auth) as c:
        assert (await c.get("/api/integration/v1/x", headers=bearer(KEY))).status_code == 200
        keys.clear()  # thu hồi
        # Trong CACHE_SECONDS vẫn dùng kết quả cũ; quên cache (process vừa thu hồi) thì hết.
        assert (await c.get("/api/integration/v1/x", headers=bearer(KEY))).status_code == 200
        auth.forget()
        assert (await c.get("/api/integration/v1/x", headers=bearer(KEY))).status_code == 401
    sessions: Any = auth.res.sessionmaker
    assert sessions.touches == 1  # `last_used_at` ghi thưa


def test_key_format_and_digest() -> None:
    key = key_service.new_key()
    assert key.startswith("jca_")
    assert len(key) > 40
    assert key_service.digest(key) != key_service.digest(key_service.new_key())
    assert len(key_service.digest(key)) == 64
