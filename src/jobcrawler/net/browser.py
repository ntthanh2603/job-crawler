"""Một Chromium (Playwright) mỗi process, chỉ khởi động ở lần dùng đầu tiên."""

from __future__ import annotations

import asyncio

from playwright.async_api import Browser, Playwright, async_playwright


class BrowserManager:
    def __init__(self, *, max_pages: int, headless: bool = True) -> None:
        # Giới hạn số trang mở đồng thời trong process (BROWSER_MAX_PAGES).
        self.pages = asyncio.Semaphore(max_pages)
        self._headless = headless
        self._lock = asyncio.Lock()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    @property
    def started(self) -> bool:
        return self._browser is not None

    async def get(self) -> Browser:
        async with self._lock:
            if self._browser is None or not self._browser.is_connected():
                if self._playwright is None:
                    self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(headless=self._headless)
            return self._browser

    async def close(self) -> None:
        async with self._lock:
            if self._browser is not None:
                await self._browser.close()
                self._browser = None
            if self._playwright is not None:
                await self._playwright.stop()
                self._playwright = None
