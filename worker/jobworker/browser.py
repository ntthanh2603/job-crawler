"""Việc render: mở trang bằng Chromium (Playwright), trả DOM đã render và JSON trang gọi.

Một browser mỗi process, khởi động ở việc render đầu tiên (WORKER_RENDER_SLOTS=0 thì không bao
giờ). Mỗi việc một context riêng (cookie/storage không lẫn giữa các việc, proxy theo context), chặn
tải ảnh, font, media. User-Agent lấy từ chính Chromium đang chạy (bỏ chữ "Headless") để khớp
fingerprint thật của browser.

Proxy theo context: Playwright 1.63 trên Windows không còn cần proxy toàn cục lúc launch (proxy giả
lúc launch còn làm context không proxy hỏng), nên launch không kèm proxy trên mọi nền tảng.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass
from typing import Any, Literal

from playwright.async_api import Browser, Playwright, Response, Route, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from jcproto.v1 import crawl_pb2
from jobworker.logging import get_logger
from jobworker.proxies import playwright_settings
from jobworker.results import elapsed_ms, encode_body, failure, task_timeout

log = get_logger(__name__)

WaitUntil = Literal["commit", "domcontentloaded", "load", "networkidle"]
WAIT_UNTIL: frozenset[str] = frozenset({"commit", "domcontentloaded", "load", "networkidle"})

BLOCKED_RESOURCES = frozenset({"image", "font", "media"})
# Bắt phản hồi JSON (`capture_json`): giới hạn để trang gọi API liên tục không ăn hết bộ nhớ.
MAX_CAPTURED_RESPONSES = 30
MAX_CAPTURED_BYTES = 2_000_000  # mỗi phản hồi
MAX_CAPTURED_TOTAL = 8_000_000
CAPTURE_DRAIN_SECONDS = 2.0  # chờ đọc xong các phản hồi đang dở sau khi trang tải xong
# `networkidle`: tải trang tới DOMContentLoaded rồi chờ mạng yên tối đa chừng này (giây). Nhiều
# site không bao giờ yên (analytics, long-polling); hết giờ thì vẫn lấy DOM hiện có.
NETWORK_IDLE_CAP_SECONDS = 8.0
_PROXY_ERRORS = ("ERR_PROXY", "ERR_TUNNEL", "ERR_SOCKS")
_TRACKERS = (
    "google-analytics",
    "googletagmanager",
    "doubleclick",
    "facebook",
    "hotjar",
    "clarity.ms",
    "sentry",
    "segment",
    "mixpanel",
    "firebase",
)


class BrowserUnavailableError(RuntimeError):
    """Không khởi động được Chromium (chưa `playwright install chromium`, thiếu thư viện...)."""


@dataclass(frozen=True, slots=True)
class Rendered:
    status: int
    url: str  # URL cuối sau redirect
    headers: dict[str, str]  # khoá viết thường
    content: bytes
    captured: tuple[crawl_pb2.CapturedJson, ...] = ()


class _JsonCapture:
    """Giữ phản hồi JSON của XHR/fetch khi trang tải (dữ liệu của trang SPA)."""

    def __init__(self) -> None:
        self.captured: list[crawl_pb2.CapturedJson] = []
        self._reads: list[asyncio.Task[None]] = []
        self._bytes = 0

    def on_response(self, response: Response) -> None:
        request = response.request
        if len(self._reads) >= MAX_CAPTURED_RESPONSES or request.resource_type not in (
            "xhr",
            "fetch",
        ):
            return
        if not response.ok or any(t in response.url for t in _TRACKERS):
            return
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type and "text/plain" not in content_type:
            return
        self._reads.append(asyncio.ensure_future(self._read(response)))

    async def _read(self, response: Response) -> None:
        try:
            body = await response.body()
        except PlaywrightError:
            return
        if len(body) > MAX_CAPTURED_BYTES or self._bytes + len(body) > MAX_CAPTURED_TOTAL:
            return
        if body.lstrip()[:1] not in (b"{", b"["):
            return
        self._bytes += len(body)
        request_json = ""
        with contextlib.suppress(Exception):
            data = response.request.post_data_json
            if data is not None:
                request_json = json.dumps(data, ensure_ascii=False)
        self.captured.append(
            crawl_pb2.CapturedJson(
                url=response.url,
                method=response.request.method,
                status=response.status,
                body=body,
                request_json=request_json,
            )
        )

    async def drain(self) -> tuple[crawl_pb2.CapturedJson, ...]:
        if self._reads:
            _, pending = await asyncio.wait(self._reads, timeout=CAPTURE_DRAIN_SECONDS)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        return tuple(self.captured)


async def _block_heavy(route: Route) -> None:
    if route.request.resource_type in BLOCKED_RESOURCES:
        await route.abort()
    else:
        await route.continue_()


def _error_kind(message: str) -> crawl_pb2.ErrorKind:
    if any(e in message for e in _PROXY_ERRORS):
        return crawl_pb2.ERROR_KIND_PROXY
    if "net::ERR_" in message:
        return crawl_pb2.ERROR_KIND_CONNECT
    return crawl_pb2.ERROR_KIND_OTHER


class BrowserManager:
    def __init__(
        self,
        *,
        worker_id: str,
        max_pages: int,
        max_body_bytes: int = 10 * 1024 * 1024,
        headless: bool = True,
    ) -> None:
        self.worker_id = worker_id
        self.max_body_bytes = max_body_bytes
        self.pages = asyncio.Semaphore(max(1, max_pages))
        self.bytes_in = 0
        self._headless = headless
        self._lock = asyncio.Lock()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self.user_agent: str | None = None

    @property
    def started(self) -> bool:
        return self._browser is not None

    def set_limits(self, *, max_pages: int, max_body_bytes: int) -> None:
        """Đổi số trang cùng lúc: trang đang mở vẫn trả slot về semaphore cũ."""
        self.max_body_bytes = max_body_bytes
        if max_pages > 0:
            self.pages = asyncio.Semaphore(max_pages)

    async def get(self) -> Browser:
        async with self._lock:
            if self._browser is None or not self._browser.is_connected():
                try:
                    if self._playwright is None:
                        self._playwright = await async_playwright().start()
                    self._browser = await self._playwright.chromium.launch(headless=self._headless)
                    self.user_agent = await self._detect_user_agent(self._browser)
                except PlaywrightError as exc:
                    raise BrowserUnavailableError(str(exc).splitlines()[0]) from exc
                log.info("worker.browser_started", user_agent=self.user_agent)
            return self._browser

    @staticmethod
    async def _detect_user_agent(browser: Browser) -> str:
        page = await browser.new_page()
        try:
            ua: str = await page.evaluate("navigator.userAgent")
        finally:
            await page.close()
        return ua.replace("HeadlessChrome/", "Chrome/")

    async def render(
        self,
        url: str,
        *,
        proxy: dict[str, str] | None,
        timeout: float,
        wait_until: WaitUntil,
        capture_json: bool = False,
        headers: dict[str, str] | None = None,
    ) -> Rendered:
        """Mở `url`; lỗi điều hướng ném `PlaywrightError` / `PlaywrightTimeoutError`."""
        browser = await self.get()
        extra = {k: v for k, v in (headers or {}).items() if k.lower() != "user-agent"}
        user_agent = next(
            (v for k, v in (headers or {}).items() if k.lower() == "user-agent"), self.user_agent
        )
        async with self.pages:
            context = await browser.new_context(
                user_agent=user_agent,
                locale="vi-VN",
                timezone_id="Asia/Ho_Chi_Minh",
                proxy=proxy,  # pyright: ignore[reportArgumentType]
                extra_http_headers=extra or None,
            )
            try:
                await context.route("**/*", _block_heavy)
                page = await context.new_page()
                capture = _JsonCapture() if capture_json else None
                if capture is not None:
                    page.on("response", capture.on_response)
                goto_wait: WaitUntil = (
                    "domcontentloaded" if wait_until == "networkidle" else wait_until
                )
                response = await page.goto(url, wait_until=goto_wait, timeout=timeout * 1000)
                if wait_until == "networkidle":
                    cap = min(NETWORK_IDLE_CAP_SECONDS, timeout) * 1000
                    with contextlib.suppress(PlaywrightTimeoutError):
                        await page.wait_for_load_state("networkidle", timeout=cap)
                html = await page.content()
                captured = await capture.drain() if capture is not None else ()
                raw_headers = await response.all_headers() if response is not None else {}
                out_headers = {k.lower(): v for k, v in raw_headers.items()}
                # DOM đã render luôn được mã hoá lại thành UTF-8, bất kể charset gốc.
                out_headers["content-type"] = "text/html; charset=utf-8"
                return Rendered(
                    status=response.status if response is not None else 200,
                    url=page.url,
                    headers=out_headers,
                    content=html.encode("utf-8"),
                    captured=captured,
                )
            finally:
                with contextlib.suppress(PlaywrightError):
                    await context.close()

    async def execute(
        self, task: crawl_pb2.FetchTask, *, proxy: str | None = None, proxy_id: str = ""
    ) -> crawl_pb2.FetchResult:
        """Một việc render → `FetchResult`. `BrowserUnavailableError` để caller tắt render."""
        started = time.monotonic()
        timeout = task_timeout(task)
        if timeout <= 0:
            return failure(
                task,
                crawl_pb2.ERROR_KIND_EXPIRED,
                "quá hạn trước khi tải",
                worker_id=self.worker_id,
                proxy_id=proxy_id,
            )
        wait_until: Any = task.wait_until if task.wait_until in WAIT_UNTIL else "domcontentloaded"
        try:
            rendered = await self.render(
                task.url,
                proxy=playwright_settings(proxy) if proxy else None,
                timeout=timeout,
                wait_until=wait_until,
                capture_json=task.capture_json,
                headers=dict(task.headers),
            )
        except BrowserUnavailableError:
            raise
        except PlaywrightTimeoutError as exc:
            kind, message = crawl_pb2.ERROR_KIND_TIMEOUT, str(exc)
        except PlaywrightError as exc:
            message = str(exc)
            kind = _error_kind(message)
        else:
            return await self._result(task, rendered, proxy_id=proxy_id, started=started)
        return failure(
            task,
            kind,
            message.splitlines()[0] if message else "lỗi Playwright",
            worker_id=self.worker_id,
            proxy_id=proxy_id,
            duration_ms=elapsed_ms(started),
        )

    async def _result(
        self, task: crawl_pb2.FetchTask, rendered: Rendered, *, proxy_id: str, started: float
    ) -> crawl_pb2.FetchResult:
        self.bytes_in += len(rendered.content)
        if len(rendered.content) > self.max_body_bytes:
            result = failure(
                task,
                crawl_pb2.ERROR_KIND_TOO_LARGE,
                f"DOM {len(rendered.content)} byte vượt {self.max_body_bytes} byte",
                worker_id=self.worker_id,
                proxy_id=proxy_id,
                duration_ms=elapsed_ms(started),
                status=rendered.status,
            )
            result.final_url = rendered.url
            return result
        body, zstd = await encode_body(rendered.content)
        return crawl_pb2.FetchResult(
            task_id=task.task_id,
            status=rendered.status,
            final_url=rendered.url,
            headers=rendered.headers,
            body=body,
            body_zstd=zstd,
            captured=rendered.captured,
            proxy_id=proxy_id,
            duration_ms=elapsed_ms(started),
            worker_id=self.worker_id,
        )

    async def close(self) -> None:
        async with self._lock:
            if self._browser is not None:
                with contextlib.suppress(PlaywrightError):
                    await self._browser.close()
                self._browser = None
            if self._playwright is not None:
                with contextlib.suppress(PlaywrightError):
                    await self._playwright.stop()
                self._playwright = None
