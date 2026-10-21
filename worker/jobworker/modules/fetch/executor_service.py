"""Chạy một việc lấy trang: kiểm hạn / loại việc / render còn bật, chọn proxy, tải bằng curl_cffi
hoặc Chromium (có hạn chặn cứng), rồi báo sức khoẻ proxy tại worker theo kết quả.

Thứ tự chọn proxy: proxy backend chỉ định (PROXY_MODE=central) → đi thẳng nếu việc yêu cầu →
proxy tại worker gắn theo domain (`LocalProxyPool`) → đi thẳng.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

from jcproto.v1 import crawl_pb2
from jobworker.modules.fetch.browser_service import BrowserManager, BrowserUnavailableError
from jobworker.modules.fetch.http_service import HttpFetcher
from jobworker.modules.fetch.render_service import RenderGate
from jobworker.modules.fetch.results_service import expired, failure, task_timeout
from jobworker.services.proxies import LocalProxyPool, Proxy, browser_compatible

# Chặn cứng: việc chạy quá timeout của nó chừng này giây thì huỷ (phòng curl/Chromium treo).
TASK_GUARD_SECONDS = 30.0


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


class TaskExecutor:
    """`FetchTask` → `FetchResult`. Không ném lỗi của việc (lỗi thành `error_kind`); trạng thái
    render ở `render` (`RenderGate`)."""

    def __init__(
        self,
        *,
        worker_id: str,
        http: HttpFetcher,
        browser: BrowserManager | None,
        proxies: LocalProxyPool,
        render: RenderGate,
    ) -> None:
        self.worker_id = worker_id
        self.http = http
        self.browser = browser
        self.proxies = proxies
        self.render = render
        # `WorkerConfig.use_local_proxies` (gateway gửi xuống): False thì không dùng proxy tại
        # worker (PROXY_MODE khác `worker`).
        self.use_local_proxies = True

    def pick_proxy(
        self, task: crawl_pb2.FetchTask, *, browser: bool
    ) -> tuple[str, str, Proxy | None]:
        """(URL proxy, proxy_id, proxy tại worker): proxy backend chỉ định → đi thẳng nếu việc
        yêu cầu → proxy tại worker gắn theo domain → đi thẳng."""
        if task.proxy:
            return task.proxy, task.proxy_id, None
        if task.direct or not self.use_local_proxies or not len(self.proxies):
            return "", "", None
        proxy = self.proxies.choose(_host(task.url), browser=browser)
        if proxy is None:
            return "", "", None
        return proxy.url, proxy.id, proxy

    def report_proxy(
        self, proxy: Proxy | None, task: crawl_pb2.FetchTask, result: crawl_pb2.FetchResult
    ) -> None:
        if proxy is None:
            return
        host = _host(task.url)
        if result.error_kind in (crawl_pb2.ERROR_KIND_PROXY, crawl_pb2.ERROR_KIND_CONNECT):
            self.proxies.report_failure(proxy, host)
        elif result.status in (403, 407, 429):
            self.proxies.report_blocked(proxy, host)
        elif result.error_kind == crawl_pb2.ERROR_KIND_NONE:
            self.proxies.report_success(proxy, host)

    async def execute(self, task: crawl_pb2.FetchTask, render: bool) -> crawl_pb2.FetchResult:
        def fail(kind: crawl_pb2.ErrorKind, error: str) -> crawl_pb2.FetchResult:
            return failure(task, kind, error, worker_id=self.worker_id)

        if expired(task):
            return fail(crawl_pb2.ERROR_KIND_EXPIRED, "quá hạn trước khi tải")
        if task.kind not in (crawl_pb2.TASK_KIND_HTTP, crawl_pb2.TASK_KIND_RENDER):
            return fail(crawl_pb2.ERROR_KIND_UNSUPPORTED, f"loại việc không hỗ trợ: {task.kind}")
        if render and (self.browser is None or self.render.off):
            return fail(crawl_pb2.ERROR_KIND_UNSUPPORTED, "worker không render (không có Chromium)")
        if render and (paused := self.render.paused()):
            return fail(
                crawl_pb2.ERROR_KIND_OTHER,
                f"render tạm tắt ({self.render.reason}), thử lại sau {paused:.0f} s",
            )
        proxy_url, proxy_id, local = self.pick_proxy(task, browser=render)
        if render and proxy_url and not browser_compatible(proxy_url):
            return fail(
                crawl_pb2.ERROR_KIND_UNSUPPORTED, "Chromium không dùng được SOCKS có mật khẩu"
            )
        try:
            async with asyncio.timeout(task_timeout(task) + TASK_GUARD_SECONDS):
                if render:
                    assert self.browser is not None
                    result = await self.browser.execute(
                        task, proxy=proxy_url or None, proxy_id=proxy_id
                    )
                    if self.browser.started:  # Chromium mở được (trang có lỗi hay không)
                        self.render.succeeded()
                else:
                    result = await self.http.execute(
                        task, proxy=proxy_url or None, proxy_id=proxy_id
                    )
        except TimeoutError:
            result = fail(crawl_pb2.ERROR_KIND_TIMEOUT, "quá thời gian chờ (worker huỷ)")
            result.proxy_id = proxy_id
        except BrowserUnavailableError as exc:
            return fail(self.render.failed(exc), f"không mở được Chromium: {exc}")
        self.report_proxy(local, task, result)
        return result
