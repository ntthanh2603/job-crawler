"""Hai vai Redis: cache thuần trên CACHE_REDIS_URL, hàng đợi / trạng thái trên REDIS_URL."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from jobcrawler.common.config import Settings
from jobcrawler.common.schemas import FetchOutcome
from jobcrawler.modules.overview import overview
from jobcrawler.services.net.http import FetchResult
from jobcrawler.services.redis.queues import Queues
from jobcrawler.services.resources import open_redis, wire_network

pytestmark = pytest.mark.usefixtures("clean_env")

PREFIX = "jc-split"


class RecordingRedis:
    """Đủ lệnh cho `RedisKV` + `RedisRateLimiter`; ghi lại khoá đã ghi / script đã đăng ký."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.data: dict[str, bytes] = {}
        self.scripts: list[str] = []

    async def get(self, key: str) -> bytes | None:
        return self.data.get(key)

    async def mget(self, keys: list[str]) -> list[bytes | None]:
        return [self.data.get(k) for k in keys]

    async def set(
        self, key: str, value: Any, px: int | None = None, ex: int | None = None, nx: bool = False
    ) -> bool | None:
        if nx and key in self.data:
            return None
        self.data[key] = value if isinstance(value, bytes) else str(value).encode()
        return True

    async def delete(self, *keys: str) -> int:
        return sum(self.data.pop(k, None) is not None for k in keys)

    async def incrby(self, key: str, amount: int = 1) -> int:
        value = int(self.data.get(key, b"0")) + amount
        self.data[key] = str(value).encode()
        return value

    def register_script(self, script: str) -> Any:
        self.scripts.append(script)

        async def run(**_: Any) -> int:
            return 0

        return run


class ExplodingRedis:
    """Redis chính trong test cache: mọi lệnh đều lỗi (cache không được chạm tới nó)."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"cache không được dùng Redis chính ({name})")


def settings(**kw: Any) -> Settings:
    return Settings(_env_file=None, redis_prefix=PREFIX, **kw)  # pyright: ignore[reportCallIssue]


def test_open_redis_shares_client_unless_cache_url_differs() -> None:
    same, same_cache = open_redis(settings())
    assert same_cache is same
    s = settings(cache_redis_url="redis://127.0.0.1:16379/0")  # trùng REDIS_URL
    main, cache = open_redis(s)
    assert cache is main
    main, cache = open_redis(settings(cache_redis_url="redis://127.0.0.1:16379/3"))
    assert cache is not main
    assert main.connection_pool.connection_kwargs["db"] == 0
    assert cache.connection_pool.connection_kwargs["db"] == 3


def test_queues_stay_on_main_redis() -> None:
    s = settings(cache_redis_url="redis://cache-host:6379/0")
    for queue in Queues.from_settings(s).all():
        assert queue._url == s.redis_url  # pyright: ignore[reportPrivateUsage]


async def test_caches_on_cache_client_state_on_main_client() -> None:
    s = settings(proxy_mode="central", proxies=["http://1.2.3.4:8080"])
    main, cache = RecordingRedis("main"), RecordingRedis("cache")
    fetch: Any = SimpleNamespace(max_body_bytes=1024)
    net = wire_network(s, redis=main, cache_redis=cache, fetch=fetch)  # pyright: ignore[reportArgumentType]

    # Cache trang HTTP (crawl và API ngoài), robots.txt, câu trả lời LLM: Redis cache.
    page = FetchResult(
        url="https://acme.vn/jobs",
        final_url="https://acme.vn/jobs",
        status=200,
        outcome=FetchOutcome.OK,
        content=b"<html></html>",
    )
    await net.http._cache.set(page)  # pyright: ignore[reportPrivateUsage]
    assert await net.api_http._cache.get(page.url) is not None  # pyright: ignore[reportPrivateUsage]
    assert net.http.robots._kv is net.cache_kv  # pyright: ignore[reportPrivateUsage]
    assert net.llm.cache is net.cache_kv
    await net.cache_kv.set("discovery:acme.vn", b"{}", ttl=60)
    assert any(k.startswith(f"{PREFIX}:http:") for k in cache.data)
    assert f"{PREFIX}:discovery:acme.vn" in cache.data

    # Trạng thái proxy pool và rate limit: Redis chính.
    proxy = await net.proxies.acquire("acme.vn")
    assert proxy is not None
    await net.proxies.report_failure(proxy, "acme.vn")
    assert any(k.startswith(f"{PREFIX}:proxy:") for k in main.data)
    assert main.scripts
    assert not cache.scripts

    assert not any(k.startswith(f"{PREFIX}:proxy:") for k in cache.data)
    assert not any(k.startswith((f"{PREFIX}:http:", f"{PREFIX}:discovery:")) for k in main.data)


async def test_overview_cache_reads_and_writes_cache_client() -> None:
    cache = RecordingRedis("cache")
    charts = {"totals": {"active_jobs": 3}}
    cache.data[overview.CHARTS_KEY.format(ns=PREFIX)] = json.dumps(charts).encode()
    res: Any = SimpleNamespace(settings=settings(), redis=ExplodingRedis(), cache_redis=cache)
    assert await overview.get_charts(res) == charts
