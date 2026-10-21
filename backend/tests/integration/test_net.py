"""Tầng mạng với Redis thật, HTTP server và proxy cục bộ (không cần Internet).

curl_cffi / Chromium nằm ở worker lấy trang (gói jobworker, có test riêng); ở đây `HttpClient`
chạy trên `DirectTransport` (httpx), còn đường qua gateway + worker ở test_gateway_e2e.py.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from redis.asyncio import Redis
from typer.testing import CliRunner

from jobcrawler.common.config import Settings, get_settings
from jobcrawler.common.schemas import FetchOutcome
from jobcrawler.modules.gateway.remote_service import RemoteRenderer, RemoteTransport
from jobcrawler.services.net.direct import DirectTransport
from jobcrawler.services.net.http import HttpClient, HttpConfig, TransportError
from jobcrawler.services.net.kv import RedisKV
from jobcrawler.services.net.proxy_pool import ProxyPool, parse_proxy_line
from jobcrawler.services.net.ratelimit import RedisRateLimiter

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
    }
    with serve_site(routes) as s:
        yield s


def _client(redis: Redis, prefix: str) -> HttpClient:
    kv = RedisKV(redis, prefix=prefix)
    return HttpClient(
        DirectTransport(),
        kv=kv,
        proxies=ProxyPool(kv, []),
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


async def test_http_client_over_real_http(redis: tuple[Redis, str], site: Site) -> None:
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


async def test_direct_transport_maps_connection_errors() -> None:
    transport = DirectTransport()
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


async def test_open_resources_proxy_modes(
    tmp_path: Path, site: Site, test_database_url: str, redis: tuple[Redis, str]
) -> None:
    from jobcrawler.services.resources import open_resources

    _, prefix = redis
    proxies_file = tmp_path / "proxies.txt"
    proxies_file.write_text("http://10.0.0.1:3128\n", encoding="utf-8")
    common: dict[str, Any] = {
        "database_url": test_database_url,
        "redis_url": _redis_url(),
        "redis_prefix": prefix,
        "proxies_file": proxies_file,
        "proxies": ["http://u:p@10.0.0.2:8080", "http://10.0.0.1:3128"],
    }
    seen: dict[str, tuple[int, bool]] = {}
    for mode in ("central", "worker", "off"):
        settings = Settings(**{**common, "proxy_mode": mode})
        async with open_resources(settings, db_logs=False) as res:
            transport = res.http.transport
            assert isinstance(transport, RemoteTransport)
            assert isinstance(res.http._renderer, RemoteRenderer)  # pyright: ignore[reportPrivateUsage]
            assert isinstance(res.api_http.transport, DirectTransport)
            assert not res.api_http.proxies.enabled
            seen[mode] = (len(res.proxies.proxies), transport.direct)
            # API bên ngoài gọi thẳng từ backend, không cần worker.
            direct = await res.api_http.fetch(f"{site.base}/hello", use_cache=False)
            assert direct.ok
            assert direct.proxy_id is None
            assert res.fetch.stats()["submitted"] == 0
    # Proxy chung (PROXIES + PROXIES_FILE, bỏ trùng) chỉ dùng khi PROXY_MODE=central.
    assert seen == {"central": (2, False), "worker": (0, False), "off": (0, True)}


def test_cli_proxies_check_reads_list_in_any_mode_and_needs_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, test_database_url: str
) -> None:
    from jobcrawler.bootstrap.cli import app

    with serve_proxy() as live:
        proxies_file = tmp_path / "proxies.txt"
        proxies_file.write_text(live.url, encoding="utf-8")
        monkeypatch.setenv("PROXIES_FILE", str(proxies_file))
        monkeypatch.setenv("DATABASE_URL", test_database_url)
        monkeypatch.setenv("REDIS_PREFIX", f"jctest:{uuid4().hex[:8]}")
        get_settings.cache_clear()
        try:
            result = CliRunner().invoke(app, ["--no-proxy", "proxies", "check", "--json"])
        finally:
            get_settings.cache_clear()
    # PROXY_MODE=off vẫn kiểm tra danh sách proxy chung. Kiểm tra chạy từ worker lấy trang:
    # không có worker thì báo lỗi nhưng không loại proxy.
    assert result.exit_code == 1, result.output
    assert "PROXY_MODE=off" in result.stderr
    [check] = json.loads(result.stdout)
    assert check["ok"] is False
    assert "no_worker" in check["error"]
    assert live.requests == []


def test_cli_proxies_check_without_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, test_database_url: str
) -> None:
    from jobcrawler.bootstrap.cli import app

    monkeypatch.setenv("PROXIES_FILE", str(tmp_path / "none.txt"))
    monkeypatch.setenv("DATABASE_URL", test_database_url)
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(app, ["proxies", "check"])
    finally:
        get_settings.cache_clear()
    assert result.exit_code == 1
    assert "Không có proxy" in result.output
