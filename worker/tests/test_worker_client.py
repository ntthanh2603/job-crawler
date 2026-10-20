from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any, cast

import grpc
import pytest
import zstandard
from worker_fakes import (  # pyright: ignore[reportMissingImports]
    PAGE,
    FakeGateway,
    ForwardProxy,
    RunningWorker,
    WebServer,
    closed_port,
    http_task,
    make_settings,
    render_task,
)

from jcproto.v1 import crawl_pb2, crawl_pb2_grpc
from jobworker import __version__, client
from jobworker.browser import BrowserUnavailableError
from jobworker.client import Backoff, RuntimeConfig, Worker, render_backoff
from jobworker.proxies import LocalProxyPool, parse_proxy_line
from jobworker.results import now_ms

Gateway = tuple[FakeGateway, int]


def start(port: int, **kw: object) -> RunningWorker:
    return RunningWorker(Worker(make_settings(port, **kw)))


async def test_register_lease_fetch_complete_leave(gateway: Gateway, web: WebServer) -> None:
    gw, port = gateway
    gw.add(http_task("t1", web.url("/redirect"), source="careers:test"))
    run = start(port, worker_pool="residential", worker_labels={"region": "hn"})
    try:
        [result] = await gw.wait_results(1)
    finally:
        await run.stop()

    [reg] = gw.registers
    assert reg.info.worker_id == "test-worker"
    assert reg.info.pool == "residential"
    assert reg.info.http_slots == 4
    assert reg.info.render_slots == 0
    assert reg.info.version == __version__
    assert dict(reg.info.labels) == {"region": "hn"}
    assert reg.info.started_at_ms > 0

    assert result.task_id == "t1"
    assert result.error_kind == crawl_pb2.ERROR_KIND_NONE
    assert result.status == 200
    assert result.final_url == web.url("/page")
    assert result.headers["x-test"] == "Yes"
    assert result.body_zstd
    assert zstandard.ZstdDecompressor().decompress(result.body) == PAGE
    assert result.worker_id == "test-worker"
    assert result.proxy_id == ""
    assert result.duration_ms >= 0

    # Lease chỉ xin đúng số slot trống; worker không render thì xin 0 việc render.
    assert gw.leases[0].http_slots == 4
    assert all(lease.render_slots == 0 for lease in gw.leases)
    assert gw.leaves == ["test-worker"]
    assert run.worker.counters.http_done == 1


async def test_expired_task_is_not_fetched(gateway: Gateway, web: WebServer) -> None:
    gw, port = gateway
    before = web.hits["/small"]
    gw.add(http_task("old", web.url("/small"), deadline_ms=now_ms() - 1000))
    run = start(port)
    try:
        [result] = await gw.wait_results(1)
    finally:
        await run.stop()
    assert result.error_kind == crawl_pb2.ERROR_KIND_EXPIRED
    assert web.hits["/small"] == before
    assert run.worker.counters.http_failed == 1


async def test_timeout_and_429_are_reported(gateway: Gateway, web: WebServer) -> None:
    gw, port = gateway
    gw.add(
        http_task("slow", web.url("/slow"), timeout_seconds=0.5),
        http_task("limited", web.url("/429")),
    )
    run = start(port)
    try:
        await gw.wait_results(2)
    finally:
        await run.stop()
    assert gw.result("slow").error_kind == crawl_pb2.ERROR_KIND_TIMEOUT
    limited = gw.result("limited")
    assert limited.error_kind == crawl_pb2.ERROR_KIND_NONE
    assert limited.status == 429
    assert limited.headers["retry-after"] == "5"


async def test_render_without_chromium_is_unsupported(gateway: Gateway, web: WebServer) -> None:
    gw, port = gateway
    gw.add(render_task("r1", web.url("/spa"), source="force"))
    run = start(port, worker_render_slots=0)
    try:
        [result] = await gw.wait_results(1)
    finally:
        await run.stop()
    assert result.error_kind == crawl_pb2.ERROR_KIND_UNSUPPORTED
    assert run.worker.browser is None  # không bao giờ tạo Playwright


async def test_not_found_on_lease_reregisters(gateway: Gateway, web: WebServer) -> None:
    gw, port = gateway
    gw.not_found_leases = 1
    gw.add(http_task("t1", web.url("/small")))
    run = start(port)
    try:
        [result] = await gw.wait_results(1)
    finally:
        await run.stop()
    assert len(gw.registers) == 2
    assert result.status == 200


async def test_gateway_restart_forgets_worker(gateway: Gateway, web: WebServer) -> None:
    gw, port = gateway
    gw.config.heartbeat_seconds = 1
    run = start(port)
    try:
        await gw.wait_for(lambda: len(gw.registers) == 1)
        gw.forget()
        await gw.wait_for(lambda: len(gw.registers) == 2)
        gw.add(http_task("t1", web.url("/small")))
        await gw.wait_results(1)
    finally:
        await run.stop()


async def test_drain_finishes_inflight_then_leaves(gateway: Gateway, web: WebServer) -> None:
    gw, port = gateway
    gw.add(http_task("slow", web.url("/slow"), timeout_seconds=10))
    run = start(port)
    await gw.wait_for(lambda: run.worker.stats().http_inflight == 1)
    gw.drain = True
    gw.add(http_task("late", web.url("/small")))  # không được nhận nữa
    await run.wait()  # worker tự thoát sau drain
    assert [r.task_id for r in gw.results] == ["slow"]
    assert gw.results[0].status == 200
    assert gw.leaves == ["test-worker"]
    assert [t.task_id for t in gw.pending] == ["late"]


async def test_stop_waits_for_inflight(gateway: Gateway, web: WebServer) -> None:
    gw, port = gateway
    gw.add(http_task("slow", web.url("/slow"), timeout_seconds=10))
    run = start(port)
    await gw.wait_for(lambda: run.worker.stats().http_inflight == 1)
    await run.stop()
    assert gw.result("slow").status == 200
    assert gw.leaves == ["test-worker"]


async def test_force_stop_cancels_inflight(gateway: Gateway, web: WebServer) -> None:
    gw, port = gateway
    gw.add(http_task("slow", web.url("/slow"), timeout_seconds=10))
    run = start(port)
    await gw.wait_for(lambda: run.worker.stats().http_inflight == 1)
    run.worker.stop("test")
    await asyncio.sleep(0.1)
    run.worker.stop("test")  # lần hai: huỷ luôn
    await run.wait(timeout=2.5)
    result = gw.result("slow")
    assert result.error_kind == crawl_pb2.ERROR_KIND_OTHER
    assert gw.leaves == ["test-worker"]


async def test_config_version_change_reregisters_and_applies(
    gateway: Gateway, web: WebServer
) -> None:
    gw, port = gateway
    gw.add(http_task("t1", web.url("/small")))
    run = start(port)
    try:
        await gw.wait_results(1)
        assert run.worker.config.http_slots == 4
        gw.config = crawl_pb2.WorkerConfig(
            version=2, http_slots=2, lease_wait_ms=150, heartbeat_seconds=1
        )
        await gw.wait_for(lambda: len(gw.registers) == 2)
        await gw.wait_for(lambda: gw.leases[-1].http_slots == 2)
        gw.add(http_task("t2", web.url("/small")))
        await gw.wait_results(2)
    finally:
        await run.stop()
    cfg = run.worker.config
    assert (cfg.version, cfg.http_slots, cfg.lease_wait_ms, cfg.heartbeat_seconds) == (2, 2, 150, 1)
    assert cfg.use_local_proxies is False
    # Đăng ký lại báo số slot đang áp dụng.
    assert gw.registers[1].info.http_slots == 4


async def test_heartbeat_sends_stats_and_drain(gateway: Gateway, web: WebServer) -> None:
    gw, port = gateway
    gw.config.heartbeat_seconds = 1
    gw.add(http_task("t1", web.url("/small")))
    run = start(port)
    await gw.wait_results(1)
    await gw.wait_for(lambda: any(h.stats.http_done == 1 for h in gw.heartbeats))
    stats = gw.heartbeats[-1].stats
    assert stats.bytes_in >= 2
    assert stats.http_inflight == 0
    gw.drain = True
    await run.wait()
    assert gw.leaves == ["test-worker"]


async def test_auth_metadata(gateway: Gateway, web: WebServer) -> None:
    gw, port = gateway
    gw.token = "bí mật".encode().hex()
    gw.add(http_task("t1", web.url("/small")))
    run = start(port, gateway_token=gw.token)
    try:
        await gw.wait_results(1)
    finally:
        await run.stop()
    assert gw.auth_failures == 0
    assert all(md["authorization"] == f"Bearer {gw.token}" for md in gw.metadata)


async def test_join_key_then_own_token_saved_and_renewed(
    gateway: Gateway, web: WebServer, tmp_path: Path
) -> None:
    gw, port = gateway
    gw.token, gw.worker_token = "jcj_khoa", "jcw_rieng"
    token_file = tmp_path / "token"
    gw.add(http_task("t1", web.url("/small")))
    run = start(port, worker_join_key="jcj_khoa", worker_token_file=token_file)
    try:
        await gw.wait_results(1)
    finally:
        await run.stop()
    # Register bằng khoá tham gia, các lời gọi sau bằng token riêng (đã lưu vào file).
    assert gw.metadata[0]["authorization"] == "Bearer jcj_khoa"
    assert {md["authorization"] for md in gw.metadata[1:]} == {"Bearer jcw_rieng"}
    assert token_file.read_text(encoding="utf-8") == "jcw_rieng"

    # Khởi động lại: dùng token đã lưu. Gateway không còn nhận token cũ (hết hạn) → bỏ token,
    # vào lại bằng khoá tham gia, nhận token mới.
    gw.metadata.clear()
    gw.worker_token = "jcw_moi"
    gw.add(http_task("t2", web.url("/small")))
    run = start(port, worker_join_key="jcj_khoa", worker_token_file=token_file)
    try:
        await gw.wait_results(2)
    finally:
        await run.stop()
    assert gw.metadata[0]["authorization"] == "Bearer jcw_rieng"  # token cũ bị từ chối
    assert gw.metadata[1]["authorization"] == "Bearer jcj_khoa"
    assert token_file.read_text(encoding="utf-8") == "jcw_moi"


async def test_wrong_token_keeps_retrying(gateway: Gateway) -> None:
    gw, port = gateway
    gw.token = "dung"
    run = start(port, gateway_token="sai")
    await gw.wait_for(lambda: gw.auth_failures >= 1)
    await asyncio.sleep(0.2)
    assert not run.task.done()  # không thoát, chờ thử lại
    await run.stop(timeout=5)
    assert gw.registers == []
    assert gw.leaves == []


async def test_gateway_down_then_up(web: WebServer) -> None:
    port = closed_port()
    run = start(port)
    await asyncio.sleep(1.0)  # đang thử lại với backoff
    assert not run.task.done()
    gw = FakeGateway()
    server = grpc.aio.server()
    crawl_pb2_grpc.add_CrawlGatewayServicer_to_server(gw, server)
    server.add_insecure_port(f"127.0.0.1:{port}")
    await server.start()
    try:
        gw.add(http_task("t1", web.url("/small")))
        await gw.wait_results(1, timeout=20)
    finally:
        await run.stop()
        await server.stop(0)


# --- Chromium lỗi: tạm tắt render rồi thử lại -----------------------------------------------------


class FlakyBrowser:
    """BrowserManager giả: `fail` lần đầu không mở được Chromium, sau đó render được."""

    def __init__(self, fail: int, *, permanent: bool = False) -> None:
        self.fail = fail
        self.permanent = permanent
        self.calls = 0
        self.bytes_in = 0
        self.started = False

    def set_limits(self, *, max_pages: int, max_body_bytes: int) -> None:
        pass

    async def execute(
        self, task: crawl_pb2.FetchTask, *, proxy: str | None = None, proxy_id: str = ""
    ) -> crawl_pb2.FetchResult:
        self.calls += 1
        if self.calls <= self.fail:
            raise BrowserUnavailableError("Target closed", permanent=self.permanent)
        self.started = True
        return crawl_pb2.FetchResult(task_id=task.task_id, status=200, body=b"<div/>")

    async def close(self) -> None:
        pass


def registered_render_slots(gw: FakeGateway) -> list[int]:
    return [r.info.render_slots for r in gw.registers]


async def test_render_backoff_then_recovers(
    gateway: Gateway, web: WebServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client, "RENDER_BACKOFF_BASE_SECONDS", 0.6)
    gw, port = gateway
    gw.config.heartbeat_seconds = 1
    browser = FlakyBrowser(fail=1)
    worker = Worker(make_settings(port, worker_render_slots=2), browser=cast(Any, browser))
    gw.add(render_task("r1", web.url("/spa")))
    run = RunningWorker(worker)
    try:
        [failed] = await gw.wait_results(1)
        assert failed.error_kind == crawl_pb2.ERROR_KIND_OTHER
        assert "Chromium" in failed.error
        # Register lại với 0 slot render trong lúc tạm tắt.
        await gw.wait_for(lambda: registered_render_slots(gw)[-1:] == [0])
        stats = worker.stats()
        assert stats.render_disabled_reason == "không mở được Chromium: Target closed"
        assert stats.render_retry_at_ms > now_ms()
        await gw.wait_for(lambda: gw.leases[-1].render_slots == 0)  # không xin việc render
        # Hết hạn: Register lại với đủ slot render, việc render chạy được, backoff về 0.
        await gw.wait_for(lambda: registered_render_slots(gw) == [2, 0, 2], timeout=5)
        gw.add(render_task("r2", web.url("/spa")))
        await gw.wait_results(2)
        assert gw.result("r2").status == 200
        await gw.wait_for(lambda: any(h.stats.render_done == 1 for h in gw.heartbeats), timeout=5)
    finally:
        await run.stop()
    stats = worker.stats()
    assert (stats.render_disabled_reason, stats.render_retry_at_ms) == ("", 0)
    assert browser.calls == 2
    assert worker._render_failures == 0  # pyright: ignore[reportPrivateUsage]


async def test_render_without_chromium_installed_stays_off(
    gateway: Gateway, web: WebServer
) -> None:
    gw, port = gateway
    browser = FlakyBrowser(fail=99, permanent=True)
    worker = Worker(make_settings(port, worker_render_slots=1), browser=cast(Any, browser))
    gw.add(render_task("r1", web.url("/spa")))
    run = RunningWorker(worker)
    try:
        [result] = await gw.wait_results(1)
        await gw.wait_for(lambda: registered_render_slots(gw) == [1, 0])
        gw.add(render_task("r2", web.url("/spa"), source="force"))  # ép giao dù xin 0
        await gw.wait_results(2)
    finally:
        await run.stop()
    assert result.error_kind == crawl_pb2.ERROR_KIND_UNSUPPORTED
    assert gw.result("r2").error_kind == crawl_pb2.ERROR_KIND_UNSUPPORTED
    assert browser.calls == 1  # không thử mở lại
    assert registered_render_slots(gw) == [1, 0]
    stats = worker.stats()
    assert stats.render_disabled_reason.startswith("không có Chromium")
    assert stats.render_retry_at_ms == 0


def test_missing_chromium_is_permanent() -> None:
    from playwright.async_api import Error as PlaywrightError

    from jobworker.browser import BrowserManager

    manager = BrowserManager(worker_id="w1", max_pages=1)
    missing = PlaywrightError("BrowserType.launch: Executable doesn't exist at /ms-playwright/x")
    assert manager._not_installed(missing)  # pyright: ignore[reportPrivateUsage]
    crashed = PlaywrightError("BrowserType.launch: Target page, context or browser has been closed")
    assert not manager._not_installed(crashed)  # pyright: ignore[reportPrivateUsage]


def test_render_backoff_grows_and_caps() -> None:
    assert [render_backoff(n) for n in range(1, 7)] == [30, 60, 120, 240, 480, 600]
    assert render_backoff(100) == 600
    assert render_backoff(0) == 30


# --- chọn proxy ----------------------------------------------------------------------------------


def pool_of(*urls: str) -> LocalProxyPool:
    proxies = [p for p in (parse_proxy_line(u) for u in urls) if p is not None]
    return LocalProxyPool(proxies, cooldown_seconds=60)


async def test_task_proxy_wins_over_local(
    gateway: Gateway, web: WebServer, forward_proxy: ForwardProxy
) -> None:
    gw, port = gateway
    dead = f"http://127.0.0.1:{closed_port()}"
    gw.add(http_task("t1", web.url("/small"), proxy=forward_proxy.url, proxy_id="central-1"))
    run = RunningWorker(Worker(make_settings(port), proxies=pool_of(dead)))
    try:
        [result] = await gw.wait_results(1)
    finally:
        await run.stop()
    assert result.status == 200
    assert result.proxy_id == "central-1"
    assert forward_proxy.requests == [web.url("/small")]


async def test_direct_task_ignores_local_proxy(gateway: Gateway, web: WebServer) -> None:
    gw, port = gateway
    dead = f"http://127.0.0.1:{closed_port()}"
    gw.add(http_task("t1", web.url("/small"), direct=True))
    run = RunningWorker(Worker(make_settings(port), proxies=pool_of(dead)))
    try:
        [result] = await gw.wait_results(1)
    finally:
        await run.stop()
    assert result.status == 200
    assert result.proxy_id == ""


async def test_local_proxy_used_and_reported(
    gateway: Gateway, web: WebServer, forward_proxy: ForwardProxy
) -> None:
    gw, port = gateway
    pool = pool_of(forward_proxy.url)
    [proxy] = pool.proxies
    gw.add(http_task("t1", web.url("/small")), http_task("t2", web.url("/403")))
    run = RunningWorker(Worker(make_settings(port), proxies=pool))
    try:
        await gw.wait_results(2)
    finally:
        await run.stop()
    assert gw.result("t1").proxy_id == proxy.id
    assert proxy.id.startswith("w:")
    assert gw.result("t2").status == 403
    assert len(forward_proxy.requests) == 2
    # 403 → proxy nghỉ với domain này: không còn proxy cho 127.0.0.1, đi thẳng.
    assert pool.choose("127.0.0.1") is None
    assert pool.choose("khac.vn") == proxy


async def test_local_proxy_failure_then_direct(gateway: Gateway, web: WebServer) -> None:
    gw, port = gateway
    pool = pool_of(f"http://127.0.0.1:{closed_port()}")
    [proxy] = pool.proxies
    gw.add(http_task("t1", web.url("/small")))
    run = RunningWorker(Worker(make_settings(port), proxies=pool))
    try:
        [failed] = await gw.wait_results(1)
        assert failed.error_kind == crawl_pb2.ERROR_KIND_PROXY
        assert failed.proxy_id == proxy.id
        gw.add(http_task("t2", web.url("/small")))
        await gw.wait_results(2)
    finally:
        await run.stop()
    retried = gw.result("t2")
    assert retried.status == 200
    assert retried.proxy_id == ""  # proxy đang nghỉ với domain này


async def test_shared_proxy_list_from_gateway(
    gateway: Gateway, web: WebServer, forward_proxy: ForwardProxy
) -> None:
    """Worker không có proxy riêng: dùng danh sách chung gateway gửi khi đăng ký; danh sách đổi
    (phiên bản mới) thì đăng ký lại và thay danh sách."""
    gw, port = gateway
    gw.config = crawl_pb2.WorkerConfig(
        version=5,
        use_local_proxies=True,
        shared_proxies=[forward_proxy.url],
        shared_proxies_hash="h1",
        lease_wait_ms=150,
        heartbeat_seconds=1,
    )
    gw.add(http_task("t1", web.url("/small")))
    run = RunningWorker(Worker(make_settings(port), proxies=pool_of()))
    try:
        [result] = await gw.wait_results(1)
        assert result.status == 200
        assert forward_proxy.requests == [web.url("/small")]
        # id như backend (không có `w:`): khớp bảng proxy ở trang Cài đặt.
        assert result.proxy_id == hashlib.sha1(forward_proxy.url.encode()).hexdigest()[:10]
        # Danh sách bị xoá (vd. đổi sang Backend chọn): worker đi thẳng.
        gw.config = crawl_pb2.WorkerConfig(
            version=6, use_local_proxies=True, lease_wait_ms=150, heartbeat_seconds=1
        )
        await gw.wait_for(lambda: len(gw.registers) == 2)
        gw.add(http_task("t2", web.url("/small")))
        await gw.wait_results(2)
    finally:
        await run.stop()
    assert gw.result("t2").proxy_id == ""
    assert len(run.worker.proxies) == 0


async def test_own_proxies_win_over_shared_list(
    gateway: Gateway, web: WebServer, forward_proxy: ForwardProxy
) -> None:
    gw, port = gateway
    gw.config = crawl_pb2.WorkerConfig(
        version=5,
        use_local_proxies=True,
        shared_proxies=[f"http://127.0.0.1:{closed_port()}"],
        shared_proxies_hash="h1",
    )
    gw.add(http_task("t1", web.url("/small")))
    pool = pool_of(forward_proxy.url)
    run = RunningWorker(Worker(make_settings(port), proxies=pool))
    try:
        [result] = await gw.wait_results(1)
    finally:
        await run.stop()
    assert result.status == 200
    assert result.proxy_id == pool.proxies[0].id  # proxy riêng của máy (`w:…`)


async def test_local_proxies_disabled_by_gateway(
    gateway: Gateway, web: WebServer, forward_proxy: ForwardProxy
) -> None:
    gw, port = gateway
    gw.config.use_local_proxies = False  # backend PROXY_MODE=central / off
    gw.add(http_task("t1", web.url("/small")))
    run = RunningWorker(Worker(make_settings(port), proxies=pool_of(forward_proxy.url)))
    try:
        [result] = await gw.wait_results(1)
    finally:
        await run.stop()
    assert result.proxy_id == ""
    assert forward_proxy.requests == []


# --- đơn vị --------------------------------------------------------------------------------------


def test_runtime_config_merge() -> None:
    local = RuntimeConfig.local(make_settings(1, worker_render_slots=2))
    merged = local.merge(crawl_pb2.WorkerConfig(version=7, render_slots=5, impersonate="chrome131"))
    assert merged.http_slots == 4  # 0 = giữ cấu hình của worker
    assert merged.render_slots == 2  # gateway là trần, không nâng máy nhỏ lên
    assert merged.impersonate == "chrome131"
    assert merged.version == 7
    assert merged.use_local_proxies is False
    lowered = local.merge(crawl_pb2.WorkerConfig(http_slots=3, render_slots=1))
    assert (lowered.http_slots, lowered.render_slots) == (3, 1)
    raised = local.merge(crawl_pb2.WorkerConfig(http_slots=64))
    assert raised.http_slots == 4
    # Worker không có Chromium: gateway không bật render được.
    no_render = RuntimeConfig.local(make_settings(1, worker_render_slots=0))
    assert no_render.merge(crawl_pb2.WorkerConfig(render_slots=3)).render_slots == 0


def test_backoff_grows_and_caps() -> None:
    backoff = Backoff(base=0.5, cap=30)
    delays = [backoff.next() for _ in range(12)]
    assert 0.5 <= delays[0] <= 0.75
    assert delays[3] > delays[0]
    assert max(delays) == pytest.approx(30)
    backoff.reset()
    assert backoff.next() <= 0.75
