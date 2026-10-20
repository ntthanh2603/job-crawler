"""Phần web không cần DB: đăng nhập/CSRF, markdown an toàn, app chỉ phục vụ API."""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from jobcrawler.config import Settings
from jobcrawler.web.app import GuardMiddleware, create_app
from jobcrawler.web.auth import TokenAuth
from jobcrawler.web.markdown import to_html


async def whoami(request: Request) -> PlainTextResponse:
    return PlainTextResponse(getattr(request.state, "user", None) or "-")


def guarded(password: str | None) -> httpx.AsyncClient:
    app = Starlette(
        routes=[
            Route("/x", whoami, methods=["GET", "POST"]),
            Route("/healthz", whoami),
        ]
    )
    auth = TokenAuth(username="admin", password=password or "", secret=b"k" * 32, hours=1)
    wrapped = GuardMiddleware(
        app, username="admin", token_auth=(lambda: auth) if password is not None else None
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=wrapped), base_url="http://ui.test")


def bearer(password: str) -> dict[str, str]:
    token = TokenAuth(username="admin", password=password, secret=b"k" * 32, hours=1).issue().token
    return {"Authorization": f"Bearer {token}"}


async def test_jwt_required_when_password_set() -> None:
    async with guarded("bí mật") as client:
        r = await client.get("/x")
        assert r.status_code == 401
        assert r.headers["www-authenticate"].startswith("Bearer")
        # Token ký với mật khẩu khác (đã đổi trong .env): hết dùng.
        assert (await client.get("/x", headers=bearer("sai"))).status_code == 401
        assert (await client.get("/x", headers={"Authorization": "Bearer !!!"})).status_code == 401
        ok = await client.get("/x", headers=bearer("bí mật"))
        assert (ok.status_code, ok.text) == (200, "admin")
        assert (await client.get("/healthz")).status_code == 200  # healthcheck không cần đăng nhập


async def test_no_password_means_open_but_csrf_still_blocked() -> None:
    async with guarded(None) as client:
        assert (await client.get("/x")).text == "admin"
        same = await client.post("/x", headers={"Origin": "http://ui.test"})
        assert same.status_code == 200
        assert (await client.post("/x")).status_code == 200  # curl/script: không có Origin
        evil = await client.post("/x", headers={"Origin": "https://evil.example"})
        assert evil.status_code == 403
        referer = await client.post("/x", headers={"Referer": "https://evil.example/page"})
        assert referer.status_code == 403
        assert (
            await client.get("/x", headers={"Origin": "https://evil.example"})
        ).status_code == 200


def test_markdown_escapes_html_and_scripts() -> None:
    html = to_html(
        "## Mô tả\n- Lập trình **Python**\n\n<script>alert(1)</script>\n\n"
        "[ứng tuyển](https://acme.vn/apply) [xấu](javascript:alert(1))"
    )
    assert "<h2>Mô tả</h2>" in html
    assert "<strong>Python</strong>" in html
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert 'target="_blank" rel="noopener" href="https://acme.vn/apply"' in html
    assert 'href="javascript:' not in html
    assert to_html(None) == to_html("") == ""


def api_client() -> httpx.AsyncClient:
    settings = Settings(_env_file=None, ui_password=SecretStr("x"))  # pyright: ignore[reportCallIssue]
    # Không chạy lifespan: các route dưới đây không cần DB; khoá ký JWT gắn tay như lifespan.
    app = create_app(settings)
    app.state.token_auth = TokenAuth(username="admin", password="x", secret=b"k" * 32, hours=1)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://ui.test",
        headers=bearer("x"),
    )


@pytest.mark.parametrize("path", ["/", "/jobs/42", "/assets/app.js", "/api/khong-co"])
async def test_api_only_unknown_paths_are_json_404(path: str) -> None:
    """Giao diện là service riêng: không trả index.html / file tĩnh, đường dẫn lạ là 404 JSON."""
    async with api_client() as client:
        r = await client.get(path)
        assert r.status_code == 404
        assert r.json() == {"detail": "Không có API này"}
        assert (await client.post(path)).status_code == 404


async def test_known_path_with_wrong_method_is_405() -> None:
    async with api_client() as client:
        assert (await client.delete("/api/me")).status_code == 405
        assert (await client.get("/healthz")).text == "ok"
