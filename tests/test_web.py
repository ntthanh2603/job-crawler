"""Phần web không cần DB: đăng nhập/CSRF, markdown an toàn, phục vụ bản build React."""

from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from jobcrawler.config import Settings
from jobcrawler.web.app import GuardMiddleware, create_app
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
    wrapped = GuardMiddleware(app, username="admin", password=password)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=wrapped), base_url="http://ui.test")


def basic(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


async def test_basic_auth_when_password_set() -> None:
    async with guarded("bí mật") as client:
        r = await client.get("/x")
        assert r.status_code == 401
        assert r.headers["www-authenticate"].startswith("Basic")
        assert (await client.get("/x", headers=basic("admin", "sai"))).status_code == 401
        assert (await client.get("/x", headers={"Authorization": "Basic !!!"})).status_code == 401
        ok = await client.get("/x", headers=basic("admin", "bí mật"))
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


def app_with_dist(tmp_path: Path, *, built: bool) -> httpx.AsyncClient:
    dist = tmp_path / "dist"
    if built:
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text("<div id=root></div>", encoding="utf-8")
        (dist / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
        (dist / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    settings = Settings(ui_dist=dist, ui_password=SecretStr("x"))
    # Không chạy lifespan: các route dưới đây không cần DB.
    app = create_app(settings)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://ui.test",
        headers=basic("admin", "x"),
    )


@pytest.mark.parametrize("path", ["/", "/jobs/42", "/companies?q=x"])
async def test_spa_routes_return_index(tmp_path: Path, path: str) -> None:
    async with app_with_dist(tmp_path, built=True) as client:
        r = await client.get(path)
        assert r.status_code == 200
        assert r.text == "<div id=root></div>"
        assert r.headers["cache-control"] == "no-cache"


async def test_spa_static_files_and_api_404(tmp_path: Path) -> None:
    async with app_with_dist(tmp_path, built=True) as client:
        assert (await client.get("/assets/app.js")).text == "console.log(1)"
        assert (await client.get("/favicon.svg")).text == "<svg/>"
        # Không lọt ra ngoài thư mục build.
        assert (await client.get("/..%2F..%2Fpyproject.toml")).text == "<div id=root></div>"
        missing = await client.get("/api/khong-co")
        assert missing.status_code == 404
        assert missing.json() == {"detail": "Không có API này"}


async def test_without_build_shows_instructions(tmp_path: Path) -> None:
    async with app_with_dist(tmp_path, built=False) as client:
        r = await client.get("/")
        assert r.status_code == 503
        assert "npm run build" in r.text
