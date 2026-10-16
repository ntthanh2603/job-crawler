"""Tầng mạng với Redis thật, curl_cffi thật, HTTP server và proxy cục bộ (không cần Internet)."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from typer.testing import CliRunner

from jobcrawler.config import Settings, get_settings
from jobcrawler.net.http import CurlTransport, HttpClient, HttpConfig, TransportError
from jobcrawler.net.kv import RedisKV
from jobcrawler.net.proxy_pool import ProxyPool, parse_proxy_line
from jobcrawler.net.ratelimit import RedisRateLimiter
from jobcrawler.schemas import FetchOutcome
from jobcrawler.services import health

from .conftest import _env
from .servers import Site, closed_port, serve_proxy, serve_site

pytestmark = pytest.mark.integration

HTML = "<html><head><meta charset='utf-8'><title>Tuyển dụng</title></head><body>Kỹ sư</body></html>"


def _redis_url() -> str:
    return _env("REDIS_URL") or Settings().redis_url


@pytest.fixture
async def redis() -> AsyncIterator[tuple[Redis, str]]:
    client = Redis.from_url(_redis_url())
    try:
        await client.ping()  # pyright: ignore[reportGeneralTypeIssues]
    except Exception:
        await client.aclose()
        pytest.skip("không kết nối được Redis")
    prefix = f"jctest:{uuid4().hex[:8]}"
    yield client, prefix
    keys = [k async for k in client.scan_iter(f"{prefix}:*")]
    if keys:
        await client.delete(*keys)
    await client.aclose()


@pytest.fixture
def site() -> Iterator[Site]:
    flaky = iter([(503, {}, b"busy")])

    def flaky_route() -> tuple[int, dict[str, str], bytes]:
        return next(flaky, (200, {"Content-Type": "text/plain"}, b"ok sau retry"))

    routes: dict[str, Any] = {
        "/robots.txt": (200, {}, b"User-agent: *\nDisallow: /private\n"),
        "/hello": (200, {"Content-Type": "text/html; charset=utf-8"}, HTML.encode()),
        "/private/1": (200, {}, b"secret"),
        "/flaky": flaky_route,
        "/trace": (200, {"Content-Type": "text/plain"}, b"fl=1\nip=127.0.0.1\nloc=VN\n"),
        "/img-page": (
            200,
            {"Content-Type": "text/html"},
            b"<html><body><img src='/img.png'><p>Trang</p></body></html>",
        ),
        "/img.png": (200, {"Content-Type": "image/png"}, b"\x89PNG"),
    }
    with serve_site(routes) as s:
        yield s


def _client(redis: Redis, prefix: str, proxies: list[str] | None = None) -> HttpClient:
    kv = RedisKV(redis, prefix=prefix)
    pool = ProxyPool(kv, [p for line in proxies or [] if (p := parse_proxy_line(line))])
    return HttpClient(
        CurlTransport(),
        kv=kv,
        proxies=pool,
        ratelimiter=RedisRateLimiter(redis, prefix=prefix),
        config=HttpConfig(default_rate=50, burst=5, backoff_base=0.01, timeout=5),
    )


async def test_redis_kv(redis: tuple[Redis, str]) -> None:
    client, prefix = redis
    kv = RedisKV(client, prefix=prefix)
    await kv.set("a", "1", ttl=0.2)
    assert await kv.set_nx("a", "2") is False
    assert await kv.mget(["a", "missing"]) == [b"1", None]
    assert await kv.incrby("n", 5) == 5
    await asyncio.sleep(0.3)
    assert await kv.get("a") is None
    await kv.delete("n")
    assert await kv.get("n") is None


async def test_redis_rate_limiter_is_shared_between_processes(redis: tuple[Redis, str]) -> None:
    client, prefix = redis
    a = RedisRateLimiter(client, prefix=prefix)
    b = RedisRateLimiter(client, prefix=prefix)  # như một worker khác
    t0 = time.monotonic()
    for limiter in (a, b, a, b):
        await limiter.acquire("site.vn", rate=10)
    assert time.monotonic() - t0 >= 0.25  # 4 token ở 10/s: chờ ≥ 3 × 0.1s

    await b.penalize("site.vn", 0.3)
    t1 = time.monotonic()
    await a.acquire("site.vn", rate=10)
    assert time.monotonic() - t1 >= 0.25


async def test_proxy_pool_state_is_shared(redis: tuple[Redis, str]) -> None:
    client, prefix = redis
    proxies = [
        p for line in ("1.1.1.1:1", "2.2.2.2:2", "3.3.3.3:3") if (p := parse_proxy_line(line))
    ]
    pool_a = ProxyPool(RedisKV(client, prefix=prefix), proxies)
    pool_b = ProxyPool(RedisKV(client, prefix=prefix), proxies)
    first = await pool_a.acquire("site.vn")
    assert first is not None
    assert await pool_b.acquire("site.vn") == first  # sticky dùng chung
    await pool_b.report_blocked(first, "site.vn")
    assert await pool_a.acquire("site.vn") != first


async def test_http_client_with_real_curl(redis: tuple[Redis, str], site: Site) -> None:
    client, prefix = redis
    http = _client(client, prefix)
    try:
        hello = await http.fetch(f"{site.base}/hello")
        assert hello.ok
        assert "Tuyển dụng" in hello.text
        assert (await http.fetch(f"{site.base}/hello")).from_cache
        assert site.hits.count("/hello") == 1

        denied = await http.fetch(f"{site.base}/private/1")
        assert denied.outcome is FetchOutcome.ROBOTS_DISALLOWED
        assert "/private/1" not in site.hits

        flaky = await http.fetch(f"{site.base}/flaky")
        assert (flaky.ok, flaky.attempts, flaky.text) == (True, 2, "ok sau retry")

        missing = await http.fetch(f"{site.base}/nope")
        assert (missing.outcome, missing.status) == (FetchOutcome.ERROR, 404)
    finally:
        await http.close()


async def test_http_client_through_proxy(redis: tuple[Redis, str], site: Site) -> None:
    client, prefix = redis
    with serve_proxy() as proxy:
        http = _client(client, prefix, [proxy.url])
        try:
            result = await http.fetch(f"{site.base}/hello")
        finally:
            await http.close()
    assert result.ok
    assert result.proxy_id is not None
    assert f"{site.base}/hello" in proxy.requests


async def test_curl_transport_maps_connection_errors() -> None:
    transport = CurlTransport()
    try:
        with pytest.raises(TransportError) as info:
            await transport.request(
                "GET",
                f"http://127.0.0.1:{closed_port()}/",
                headers={},
                proxy=None,
                timeout=3,
                allow_redirects=True,
            )
        assert info.value.kind == "connect"
    finally:
        await transport.close()


async def test_check_proxies(redis: tuple[Redis, str], site: Site) -> None:
    client, prefix = redis
    with serve_proxy() as live:
        dead_url = f"http://127.0.0.1:{closed_port()}"
        http = _client(client, prefix, [live.url, dead_url])
        res = SimpleNamespace(
            settings=Settings(proxy_check_url=f"{site.base}/trace"),
            proxies=http.proxies,
            http=http,
        )
        try:
            results = await health.check_proxies(cast(Any, res), timeout=3)
        finally:
            await http.close()
    by_url = {r.proxy: r for r in results}
    ok = by_url[live.url]
    assert (ok.ok, ok.exit_ip, ok.location) == (True, "127.0.0.1", "VN")
    failed = by_url[dead_url]
    assert not failed.ok
    assert failed.error is not None
    assert failed.error.startswith("connect")
    states = {s.proxy.url: s for s in await http.proxies.states()}
    assert not states[live.url].dead
    assert states[dead_url].dead


def test_cli_proxies_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, site: Site, test_database_url: str
) -> None:
    from jobcrawler.entrypoints.cli import app

    with serve_proxy() as live:
        proxies_file = tmp_path / "proxies.txt"
        proxies_file.write_text(f"{live.url}\n127.0.0.1:{closed_port()}\n", encoding="utf-8")
        monkeypatch.setenv("PROXIES_FILE", str(proxies_file))
        monkeypatch.setenv("PROXY_CHECK_URL", f"{site.base}/trace")
        monkeypatch.setenv("DATABASE_URL", test_database_url)
        monkeypatch.setenv("REDIS_PREFIX", f"jctest:{uuid4().hex[:8]}")
        get_settings.cache_clear()
        try:
            result = CliRunner().invoke(app, ["proxies", "check", "--json", "--timeout", "3"])
        finally:
            get_settings.cache_clear()
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert sorted(r["ok"] for r in data) == [False, True]


def test_cli_proxies_check_without_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, test_database_url: str
) -> None:
    from jobcrawler.entrypoints.cli import app

    monkeypatch.setenv("PROXIES_FILE", str(tmp_path / "none.txt"))
    monkeypatch.setenv("DATABASE_URL", test_database_url)
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(app, ["proxies", "check"])
    finally:
        get_settings.cache_clear()
    assert result.exit_code == 1
    assert "Không có proxy" in result.output


async def test_browser_render_blocks_images(site: Site) -> None:
    from playwright.async_api import Error as PlaywrightError

    from jobcrawler.net.browser import BrowserManager

    browser = BrowserManager(max_pages=2)
    try:
        try:
            await browser.get()
        except PlaywrightError as exc:
            pytest.skip(f"chưa cài Chromium cho Playwright: {str(exc).splitlines()[0]}")
        raw = await browser.render(
            f"{site.base}/img-page", proxy=None, timeout=10, wait_until="load"
        )
    finally:
        await browser.close()
    assert raw.status == 200
    assert "<p>Trang</p>" in raw.content.decode()
    assert "/img.png" not in site.hits
    assert browser.user_agent is not None
    assert "Headless" not in browser.user_agent
