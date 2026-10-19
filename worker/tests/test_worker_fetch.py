from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
import zstandard
from worker_fakes import (  # pyright: ignore[reportMissingImports]
    BIG_BYTES,
    PAGE,
    ForwardProxy,
    WebServer,
    closed_port,
    http_task,
)

from jcproto.v1 import crawl_pb2
from jobworker.fetch import HttpFetcher
from jobworker.results import now_ms


@pytest.fixture
async def fetcher() -> AsyncIterator[HttpFetcher]:
    f = HttpFetcher(worker_id="w1", max_clients=4, max_body_bytes=64 * 1024)
    yield f
    await f.close()


def body_of(result: crawl_pb2.FetchResult) -> bytes:
    if result.body_zstd:
        return zstandard.ZstdDecompressor().decompress(result.body)
    return result.body


async def test_redirect_headers_and_zstd_body(fetcher: HttpFetcher, web: WebServer) -> None:
    result = await fetcher.execute(http_task("t1", web.url("/redirect")))
    assert result.error_kind == crawl_pb2.ERROR_KIND_NONE
    assert result.status == 200
    assert result.final_url == web.url("/page")
    assert result.headers["x-test"] == "Yes"
    assert all(k == k.lower() for k in result.headers)
    assert result.body_zstd is True  # trang > 2 KiB
    assert len(result.body) < len(PAGE)
    assert body_of(result) == PAGE
    assert result.worker_id == "w1"
    assert result.task_id == "t1"
    assert fetcher.bytes_in >= len(PAGE)


async def test_small_body_not_compressed_and_no_redirect(
    fetcher: HttpFetcher, web: WebServer
) -> None:
    result = await fetcher.execute(http_task("t", web.url("/redirect"), allow_redirects=False))
    assert result.status == 302
    assert result.headers["location"] == "/page"
    small = await fetcher.execute(http_task("t", web.url("/small")))
    assert small.body == b"ok"
    assert small.body_zstd is False


async def test_post_body_and_headers(fetcher: HttpFetcher, web: WebServer) -> None:
    task = http_task(
        "t",
        web.url("/echo"),
        method="post",
        body=b'{"q": "k\xe1\xbb\xb9 s\xc6\xb0"}',
        headers={"Content-Type": "application/json", "X-Custom": "1"},
    )
    result = await fetcher.execute(task)
    echo = json.loads(body_of(result))
    assert echo["method"] == "POST"
    assert echo["body"] == '{"q": "kỹ sư"}'
    assert echo["headers"]["content-type"] == "application/json"
    assert echo["headers"]["x-custom"] == "1"
    assert "Chrome" in echo["headers"]["user-agent"]  # impersonate Chrome


async def test_timeout(fetcher: HttpFetcher, web: WebServer) -> None:
    result = await fetcher.execute(http_task("t", web.url("/slow"), timeout_seconds=0.5))
    assert result.error_kind == crawl_pb2.ERROR_KIND_TIMEOUT
    assert result.status == 0
    assert result.duration_ms >= 400


async def test_deadline_shortens_timeout(fetcher: HttpFetcher, web: WebServer) -> None:
    task = http_task("t", web.url("/slow"), timeout_seconds=30, deadline_ms=now_ms() + 500)
    result = await fetcher.execute(task)
    assert result.error_kind == crawl_pb2.ERROR_KIND_TIMEOUT
    assert result.duration_ms < 2500


async def test_expired_is_not_fetched(fetcher: HttpFetcher, web: WebServer) -> None:
    before = web.hits["/small"]
    task = http_task("t", web.url("/small"), deadline_ms=now_ms() - 1)
    result = await fetcher.execute(task)
    assert result.error_kind == crawl_pb2.ERROR_KIND_EXPIRED
    assert web.hits["/small"] == before


@pytest.mark.parametrize("path", ["/big", "/big-chunked"])
async def test_too_large(fetcher: HttpFetcher, web: WebServer, path: str) -> None:
    assert fetcher.max_body_bytes < BIG_BYTES
    result = await fetcher.execute(http_task("t", web.url(path)))
    assert result.error_kind == crawl_pb2.ERROR_KIND_TOO_LARGE
    assert result.body == b""


async def test_reconfigure_body_limit(fetcher: HttpFetcher, web: WebServer) -> None:
    fetcher.reconfigure(impersonate="chrome", max_clients=4, max_body_bytes=1024 * 1024)
    ok = await fetcher.execute(http_task("t", web.url("/big")))
    assert ok.status == 200
    assert len(body_of(ok)) == BIG_BYTES
    # Đổi số client: session mới, vẫn tải được.
    fetcher.reconfigure(impersonate="chrome", max_clients=8, max_body_bytes=1024 * 1024)
    again = await fetcher.execute(http_task("t", web.url("/small")))
    assert again.status == 200


async def test_connect_error(fetcher: HttpFetcher) -> None:
    result = await fetcher.execute(http_task("t", f"http://127.0.0.1:{closed_port()}/"))
    assert result.error_kind == crawl_pb2.ERROR_KIND_CONNECT
    assert result.status == 0


async def test_proxy_error(fetcher: HttpFetcher, web: WebServer) -> None:
    dead = f"http://127.0.0.1:{closed_port()}"
    result = await fetcher.execute(http_task("t", web.url("/small")), proxy=dead, proxy_id="p1")
    assert result.error_kind == crawl_pb2.ERROR_KIND_PROXY
    assert result.proxy_id == "p1"


async def test_through_proxy_with_auth(fetcher: HttpFetcher, web: WebServer) -> None:
    proxy = ForwardProxy("u", "p@ss")
    try:
        url = proxy.url.replace("p@ss", "p%40ss")
        result = await fetcher.execute(http_task("t", web.url("/small")), proxy=url, proxy_id="x")
    finally:
        proxy.close()
    assert result.status == 200
    assert result.body == b"ok"
    assert proxy.requests == [web.url("/small")]
