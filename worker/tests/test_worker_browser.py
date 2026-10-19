"""Render bằng Chromium thật (Playwright). Tự bỏ qua khi chưa `playwright install chromium`."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator

import pytest
import zstandard
from worker_fakes import (  # pyright: ignore[reportMissingImports]
    FakeGateway,
    ForwardProxy,
    RunningWorker,
    WebServer,
    make_settings,
    render_task,
)

from jcproto.v1 import crawl_pb2
from jobworker.browser import BrowserManager
from jobworker.client import Worker
from jobworker.results import now_ms


def _chromium_installed() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return os.path.exists(p.chromium.executable_path)
    except Exception:
        return False


pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(not _chromium_installed(), reason="chưa cài Chromium của Playwright"),
]


@pytest.fixture
async def browser() -> AsyncIterator[BrowserManager]:
    manager = BrowserManager(worker_id="w1", max_pages=2)
    yield manager
    await manager.close()


def body_of(result: crawl_pb2.FetchResult) -> str:
    raw = zstandard.ZstdDecompressor().decompress(result.body) if result.body_zstd else result.body
    return raw.decode()


async def test_render_captures_json(browser: BrowserManager, web: WebServer) -> None:
    task = render_task("r1", web.url("/spa"), capture_json=True, wait_until="networkidle")
    result = await browser.execute(task)
    assert result.error_kind == crawl_pb2.ERROR_KIND_NONE, result.error
    assert result.status == 200
    assert result.final_url == web.url("/spa")
    assert result.headers["content-type"] == "text/html; charset=utf-8"
    assert "jobs:2" in body_of(result)
    [captured] = result.captured
    assert captured.url == web.url("/api/jobs")
    assert captured.method == "POST"
    assert captured.status == 200
    assert json.loads(captured.body)["jobs"][0]["title"] == "Kỹ sư"
    assert json.loads(captured.request_json) == {"q": "x"}
    assert browser.user_agent is not None
    assert "Headless" not in browser.user_agent


async def test_render_through_proxy_with_auth(browser: BrowserManager, web: WebServer) -> None:
    proxy = ForwardProxy("u", "p")
    try:
        result = await browser.execute(
            render_task("r1", web.url("/page")), proxy=proxy.url, proxy_id="central-1"
        )
    finally:
        proxy.close()
    assert result.error_kind == crawl_pb2.ERROR_KIND_NONE, result.error
    assert result.proxy_id == "central-1"
    assert web.url("/page") in proxy.requests


async def test_render_errors(browser: BrowserManager, web: WebServer) -> None:
    expired = await browser.execute(render_task("r1", web.url("/page"), deadline_ms=now_ms() - 1))
    assert expired.error_kind == crawl_pb2.ERROR_KIND_EXPIRED
    slow = await browser.execute(render_task("r2", web.url("/slow"), timeout_seconds=1))
    assert slow.error_kind == crawl_pb2.ERROR_KIND_TIMEOUT
    dead = "http://127.0.0.1:9"
    proxied = await browser.execute(render_task("r3", web.url("/page")), proxy=dead)
    assert proxied.error_kind == crawl_pb2.ERROR_KIND_PROXY


async def test_worker_render_end_to_end(gateway: tuple[FakeGateway, int], web: WebServer) -> None:
    gw, port = gateway
    gw.add(render_task("r1", web.url("/spa"), capture_json=True))
    run = RunningWorker(Worker(make_settings(port, worker_render_slots=1)))
    try:
        [result] = await gw.wait_results(1, timeout=60)
    finally:
        await run.stop()
    assert result.error_kind == crawl_pb2.ERROR_KIND_NONE, result.error
    assert gw.registers[0].info.render_slots == 1
    assert any(lease.render_slots == 1 for lease in gw.leases)
    assert run.worker.counters.render_done == 1
