"""Chromium (Playwright): một browser mỗi process, khởi động ở lần dùng đầu tiên.

Mỗi lần render mở một context riêng (cookie/storage không lẫn giữa các job), chặn tải ảnh,
font, media; tối đa `BROWSER_MAX_PAGES` trang cùng lúc. User-Agent lấy từ chính Chromium
đang chạy (bỏ chữ "Headless") để khớp fingerprint thật của browser.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from typing import Any

from playwright.async_api import Browser, Playwright, Response, Route, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from jobcrawler.net.http import CapturedJson, RawResponse, TransportError, WaitUntil
from jobcrawler.net.proxy_pool import Proxy

BLOCKED_RESOURCES = frozenset({"image", "font", "media"})
# Bắt phản hồi JSON (`capture_json`): giới hạn để trang gọi API liên tục không ăn hết bộ nhớ.
MAX_CAPTURED_RESPONSES = 30
MAX_CAPTURED_BYTES = 2_000_000  # mỗi phản hồi
MAX_CAPTURED_TOTAL = 8_000_000
CAPTURE_DRAIN_SECONDS = 2.0  # chờ đọc xong các phản hồi đang dở sau khi trang tải xong
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


class _JsonCapture:
    """Giữ phản hồi JSON của XHR/fetch khi trang tải (dữ liệu của trang SPA)."""

    def __init__(self) -> None:
        self.captured: list[CapturedJson] = []
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
        request_json: Any = None
        with contextlib.suppress(Exception):
            request_json = response.request.post_data_json
        self.captured.append(
            CapturedJson(
                url=response.url,
                method=response.request.method,
                status=response.status,
                body=body,
                request_json=request_json,
            )
        )

    async def drain(self) -> tuple[CapturedJson, ...]:
        if self._reads:
            _, pending = await asyncio.wait(self._reads, timeout=CAPTURE_DRAIN_SECONDS)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        return tuple(self.captured)


# `networkidle`: tải trang tới DOMContentLoaded rồi chờ mạng yên tối đa chừng này (giây). Nhiều
# site không bao giờ yên (analytics, long-polling); hết giờ thì vẫn lấy DOM hiện có.
NETWORK_IDLE_CAP_SECONDS = 8.0
_PROXY_ERRORS = ("ERR_PROXY", "ERR_TUNNEL", "ERR_SOCKS")


async def _block_heavy(route: Route) -> None:
    if route.request.resource_type in BLOCKED_RESOURCES:
        await route.abort()
    else:
        await route.continue_()


class BrowserManager:
    def __init__(self, *, max_pages: int, headless: bool = True, use_proxies: bool = False) -> None:
        self.pages = asyncio.Semaphore(max_pages)
        self._headless = headless
        self._use_proxies = use_proxies
        self._lock = asyncio.Lock()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self.user_agent: str | None = None

    @property
    def started(self) -> bool:
        return self._browser is not None

    async def get(self) -> Browser:
        async with self._lock:
            if self._browser is None or not self._browser.is_connected():
                if self._playwright is None:
                    self._playwright = await async_playwright().start()
                launch: dict[str, Any] = {"headless": self._headless}
                if self._use_proxies and sys.platform == "win32":
                    # Chromium trên Windows chỉ nhận proxy theo context khi có proxy toàn cục.
                    launch["proxy"] = {"server": "http://per-context"}
                self._browser = await self._playwright.chromium.launch(**launch)
                self.user_agent = await self._detect_user_agent(self._browser)
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
        proxy: Proxy | None,
        timeout: float,
        wait_until: WaitUntil,
        capture_json: bool = False,
    ) -> RawResponse:
        browser = await self.get()
        async with self.pages:
            context = await browser.new_context(
                user_agent=self.user_agent,
                locale="vi-VN",
                timezone_id="Asia/Ho_Chi_Minh",
                proxy=proxy.playwright() if proxy else None,  # pyright: ignore[reportArgumentType]
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
                try:
                    response = await page.goto(url, wait_until=goto_wait, timeout=timeout * 1000)
                    if wait_until == "networkidle":
                        cap = min(NETWORK_IDLE_CAP_SECONDS, timeout) * 1000
                        with contextlib.suppress(PlaywrightTimeoutError):
                            await page.wait_for_load_state("networkidle", timeout=cap)
                except PlaywrightTimeoutError as exc:
                    raise TransportError("timeout", str(exc)) from exc
                except PlaywrightError as exc:
                    message = str(exc)
                    kind = "proxy" if any(e in message for e in _PROXY_ERRORS) else "connect"
                    raise TransportError(kind, message) from exc
                html = await page.content()
                captured = await capture.drain() if capture is not None else ()
                raw_headers = await response.all_headers() if response is not None else {}
                headers = {k.lower(): v for k, v in raw_headers.items()}
                # DOM đã render luôn được mã hoá lại thành UTF-8, bất kể charset gốc.
                headers["content-type"] = "text/html; charset=utf-8"
                return RawResponse(
                    status=response.status if response is not None else 200,
                    url=page.url,
                    headers=headers,
                    content=html.encode("utf-8"),
                    captured=captured,
                )
            finally:
                await context.close()

    async def close(self) -> None:
        async with self._lock:
            if self._browser is not None:
                await self._browser.close()
                self._browser = None
            if self._playwright is not None:
                await self._playwright.stop()
                self._playwright = None
