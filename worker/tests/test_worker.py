from __future__ import annotations

import asyncio
import hashlib
import sys
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
    stop_gateway,
)

from jcproto.v1 import crawl_pb2, crawl_pb2_grpc
from jobworker import __version__
from jobworker.common.backoff import Backoff
from jobworker.modules.fetch import browser_service, render_service
from jobworker.modules.fetch.browser_service import BrowserUnavailableError
from jobworker.modules.fetch.results_service import now_ms
from jobworker.modules.gateway import worker_service
from jobworker.modules.gateway.runtime_service import RuntimeConfig
from jobworker.modules.gateway.worker_service import RegisterRejectedError, Worker
from jobworker.services.proxies import LocalProxyPool, parse_proxy_line

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


@pytest.mark.parametrize("stopping", [False, True])
async def test_heartbeat_continues_while_reregister_pending(
    gateway: Gateway, web: WebServer, stopping: bool
) -> None:
    """Mọi slot đang bận mà cấu hình đổi (kể cả lúc đang dừng êm): Register lại không chờ slot
    trống, Heartbeat không ngưng — gateway không được thấy worker quá hạn báo sống khi việc còn
    chạy (việc sẽ bị trả `worker_lost`)."""
    gw, port = gateway
    gw.config.heartbeat_seconds = 1
    gw.add(http_task("slow", web.url("/slow"), timeout_seconds=10))
    run = start(port, worker_http_slots=1)
    try:
        await gw.wait_for(lambda: run.worker.stats().http_inflight == 1)
        if stopping:
            run.worker.stop("test")
        gw.config.version = 2
        seen = len(gw.heartbeats)
        # /slow trả sau 3 s: hai nhịp báo sống (1 s) phải tới trong lúc việc còn chạy.
        await gw.wait_for(lambda: len(gw.heartbeats) >= seen + 2, timeout=2.5)
        assert run.worker.stats().http_inflight == 1
        assert len(gw.registers) == (1 if stopping else 2)
    finally:
        await (run.wait() if stopping else run.stop())  # stop() lần hai là dừng cưỡng bức
    assert gw.result("slow").status == 200


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


async def test_heartbeat_reports_owed_tasks_and_cancels_revoked(
    gateway: Gateway, web: WebServer
) -> None:
    gw, port = gateway
    gw.config.heartbeat_seconds = 1
    gw.add(http_task("slow", web.url("/slow"), timeout_seconds=10))
    run = start(port)
    try:
        # Việc đang chạy được kể trong Heartbeat để gateway đối soát.
        await gw.wait_for(lambda: any(list(h.task_ids) == ["slow"] for h in gw.heartbeats))
        assert all(h.reports_tasks for h in gw.heartbeats)
        # Gateway đã thu hồi việc (đã trả worker_lost cho backend): worker huỷ ngay, không chờ
        # trang chậm (3 s) tải xong.
        gw.revoke.add("slow")
        [result] = await gw.wait_results(1, timeout=2.5)
        assert result.error_kind == crawl_pb2.ERROR_KIND_OTHER
        assert result.error == "gateway đã thu hồi việc"
        # Complete xong thì thôi kể việc này.
        seen = len(gw.heartbeats)
        await gw.wait_for(
            lambda: len(gw.heartbeats) > seen and list(gw.heartbeats[-1].task_ids) == []
        )
    finally:
        await run.stop()


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


async def test_token_bound_to_other_process_reregisters_with_same_token(
    gateway: Gateway, web: WebServer, tmp_path: Path
) -> None:
    """Process khác dùng chung WORKER_TOKEN_FILE vừa Register bằng token đó (gateway gắn token với
    worker_id của nó) → PERMISSION_DENIED: Register lại bằng chính token đó (gắn lại), không xoá
    file token, không quay về khoá tham gia đã dùng."""
    gw, port = gateway
    gw.token, gw.worker_token = "jcj_khoa_moi", "jcw_rieng"
    token_file = tmp_path / "token"
    token_file.write_text("jcw_rieng", encoding="utf-8")
    run = start(port, worker_join_key="jcj_da_dung", worker_token_file=token_file)
    try:
        await gw.wait_for(lambda: len(gw.registers) == 1)
        gw.token_owner = "process-khac"
        await gw.wait_for(lambda: len(gw.registers) == 2)
        gw.add(http_task("t1", web.url("/small")))
        await gw.wait_results(1)
    finally:
        await run.stop()
    assert gw.permission_denied >= 1
    assert gw.auth_failures == 0
    assert {md["authorization"] for md in gw.metadata} == {"Bearer jcw_rieng"}
    assert token_file.read_text(encoding="utf-8") == "jcw_rieng"


async def test_register_invalid_argument_is_fatal(gateway: Gateway) -> None:
    """Gateway từ chối WORKER_ID / WORKER_POOL (INVALID_ARGUMENT): lỗi cấu hình, thử lại vô ích —
    worker thoát thay vì thử lại mãi như mất kết nối."""
    gw, port = gateway
    gw.register_errors = [grpc.StatusCode.INVALID_ARGUMENT] * 10
    run = start(port)
    with pytest.raises(RegisterRejectedError, match="Register bị từ chối"):
        await run.wait(timeout=5)
    assert len(gw.register_errors) == 9  # không thử lại
    assert gw.leaves == []


async def test_oversized_result_becomes_too_large(
    gateway: Gateway, web: WebServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Một kết quả vượt trần message gRPC thành lỗi TOO_LARGE ngay ở worker, không kéo cả lô
    Complete hỏng theo."""
    monkeypatch.setattr(worker_service, "MAX_RESULT_BYTES", 32 * 1024)
    gw, port = gateway
    gw.add(http_task("noise", web.url("/noise")), http_task("small", web.url("/small")))
    run = start(port)
    try:
        await gw.wait_results(2)
    finally:
        await run.stop()
    big = gw.result("noise")
    assert big.error_kind == crawl_pb2.ERROR_KIND_TOO_LARGE
    assert big.status == 200
    assert not big.body
    assert gw.result("small").status == 200
    assert run.worker.counters.http_failed == 1


async def test_complete_resource_exhausted_not_retried(gateway: Gateway, web: WebServer) -> None:
    """RESOURCE_EXHAUSTED ở Complete (lô vượt trần message) gửi lại vẫn hỏng: không thử lại."""
    gw, port = gateway
    gw.complete_errors = [grpc.StatusCode.RESOURCE_EXHAUSTED]
    gw.add(http_task("t1", web.url("/small")))
    run = start(port)
    try:
        await gw.wait_for(lambda: gw.completes == 1)
        await asyncio.sleep(0.8)  # lần thử lại đầu (nếu có) sau 0.5 s
    finally:
        await run.stop()
    assert gw.completes == 1
    assert gw.results == []


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
        await stop_gateway(server)


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
    monkeypatch.setattr(render_service, "RENDER_BACKOFF_BASE_SECONDS", 0.6)
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
    assert worker.render.failures == 0


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

    from jobworker.modules.fetch.browser_service import BrowserManager

    manager = BrowserManager(worker_id="w1", max_pages=1)
    missing = PlaywrightError("BrowserType.launch: Executable doesn't exist at /ms-playwright/x")
    assert manager._not_installed(missing)  # pyright: ignore[reportPrivateUsage]
    crashed = PlaywrightError("BrowserType.launch: Target page, context or browser has been closed")
    assert not manager._not_installed(crashed)  # pyright: ignore[reportPrivateUsage]


class FakeChromium:
    """`async_playwright().start()` giả (kiêm `.chromium`): `dead` = driver node đã chết, mọi
    `launch` lỗi; `ua_error` = browser đầu tiên mở được nhưng hỏng khi đọc User-Agent."""

    executable_path = sys.executable  # file có thật: lỗi launch không bị coi là chưa cài

    def __init__(self, *, dead: bool = False, ua_error: bool = False) -> None:
        self.dead = dead
        self.ua_error = ua_error
        self.chromium = self
        self.browsers: list[FakeBrowser] = []
        self.stopped = False

    async def launch(self, *, headless: bool) -> FakeBrowser:
        from playwright.async_api import Error as PlaywrightError

        if self.dead:
            raise PlaywrightError("BrowserType.launch: Connection closed while reading from driver")
        browser = FakeBrowser(ua_error=self.ua_error and not self.browsers)
        self.browsers.append(browser)
        return browser

    async def stop(self) -> None:
        self.stopped = True


class FakeBrowser:
    def __init__(self, *, ua_error: bool) -> None:
        self.ua_error = ua_error
        self.closed = False

    def is_connected(self) -> bool:
        return not self.closed

    async def new_page(self) -> FakePage:
        return FakePage(self.ua_error)

    async def close(self) -> None:
        self.closed = True


class FakePage:
    def __init__(self, ua_error: bool) -> None:
        self.ua_error = ua_error

    async def evaluate(self, expression: str) -> str:
        from playwright.async_api import Error as PlaywrightError

        if self.ua_error:
            raise PlaywrightError("Target page, context or browser has been closed")
        return "Mozilla/5.0 HeadlessChrome/140.0.0.0 Safari/537.36"

    async def close(self) -> None:
        pass


def scripted_playwright(monkeypatch: pytest.MonkeyPatch, *drivers: FakeChromium) -> None:
    """Mỗi lần khởi động Playwright lấy driver kế tiếp trong `drivers`."""
    queue = list(drivers)

    class Starter:
        async def start(self) -> FakeChromium:
            return queue.pop(0)

    monkeypatch.setattr(browser_service, "async_playwright", Starter)


async def test_dead_playwright_driver_is_restarted(monkeypatch: pytest.MonkeyPatch) -> None:
    dead, fresh = FakeChromium(dead=True), FakeChromium()
    scripted_playwright(monkeypatch, dead, fresh)
    manager = browser_service.BrowserManager(worker_id="w1", max_pages=1)
    with pytest.raises(BrowserUnavailableError) as exc:
        await manager.get()
    assert not exc.value.permanent
    assert dead.stopped  # driver chết bị bỏ, lần sau khởi động driver mới
    assert not manager.started
    await manager.get()
    assert manager.started
    assert manager.user_agent == "Mozilla/5.0 Chrome/140.0.0.0 Safari/537.36"
    await manager.close()
    assert fresh.stopped


async def test_user_agent_failure_does_not_keep_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mở được Chromium nhưng không đọc được User-Agent: không giữ browser đó (sẽ chạy với UA
    "HeadlessChrome"), lần sau mở lại từ đầu."""
    first, second = FakeChromium(ua_error=True), FakeChromium()
    scripted_playwright(monkeypatch, first, second)
    manager = browser_service.BrowserManager(worker_id="w1", max_pages=1)
    with pytest.raises(BrowserUnavailableError):
        await manager.get()
    assert not manager.started
    assert manager.user_agent is None
    assert first.browsers[0].closed
    assert first.stopped
    browser = await manager.get()
    assert browser is second.browsers[0]
    assert manager.user_agent is not None
    assert "Headless" not in manager.user_agent
    await manager.close()


def test_headless_shell_counts_as_installed(tmp_path: Path) -> None:
    """Image Docker chỉ có Chromium headless shell: lỗi mở Chromium khác (thiếu RAM...) không bị
    coi là chưa cài (tắt hẳn render)."""
    from jobworker.modules.fetch.browser_service import chromium_installed

    full = tmp_path / "chromium-1243" / "chrome-linux64" / "chrome"
    assert not chromium_installed(str(full))
    shell = tmp_path / "chromium_headless_shell-1243" / "chrome-headless-shell-linux64"
    shell.mkdir(parents=True)
    (shell / "chrome-headless-shell").write_bytes(b"")
    assert chromium_installed(str(full))
    full.parent.mkdir(parents=True)
    full.write_bytes(b"")
    assert chromium_installed(str(full))


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
    assert max(delays) <= 30
    # Ở mức trần vẫn có jitter (20–30 s): các worker mất gateway cùng lúc không thử lại cùng nhịp.
    capped = [backoff.next() for _ in range(50)]
    assert all(20 <= d <= 30 for d in capped)
    assert max(capped) - min(capped) > 1
    backoff.reset()
    assert backoff.next() <= 0.75
