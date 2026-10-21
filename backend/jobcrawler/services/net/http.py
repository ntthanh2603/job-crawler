"""HTTP client: robots + cache + proxy pool + rate limit + retry + nhận diện trang chặn.

Request đi qua `Transport` / `Renderer`: khi crawl là worker lấy trang
(`modules.gateway.remote_service`, worker gửi bằng curl_cffi / Chromium), khi gọi API bên ngoài là
httpx (`services.net.direct`). Các nguồn (sources/) chỉ phụ thuộc giao diện `Fetcher`; test inject
`FakeFetcher` (fakes.py). Bản thân `HttpClient` test được bằng `FakeTransport`/`FakeRenderer`."""

from __future__ import annotations

import asyncio
import email.utils
import hashlib
import json as jsonlib
import random
import re
import time
import zlib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import cached_property
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import urlencode, urlsplit

from jobcrawler.common.logging import get_logger
from jobcrawler.common.schemas import FetchOutcome
from jobcrawler.services.net.blocking import detect_block
from jobcrawler.services.net.kv import KeyValueStore
from jobcrawler.services.net.proxy_pool import NoProxyAvailableError, Proxy, ProxyPool
from jobcrawler.services.net.ratelimit import RateLimiter, Sleep
from jobcrawler.services.net.robots import RobotsCache, RobotsRules

if TYPE_CHECKING:
    from jobcrawler.common.config import Settings

log = get_logger(__name__)

WaitUntil = Literal["commit", "domcontentloaded", "load", "networkidle"]
TransportErrorKind = Literal["timeout", "proxy", "connect", "other"]

RETRY_STATUSES = frozenset({500, 502, 503, 504, 520, 521, 522, 523, 524})
_META_CHARSET = re.compile(rb"""<meta[^>]+charset=["']?\s*([a-zA-Z0-9_\-]+)""", re.IGNORECASE)
_HEADER_CHARSET = re.compile(r"charset=([^\s;]+)", re.IGNORECASE)


# --- tầng truyền tải ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CapturedJson:
    """Phản hồi JSON (XHR/fetch) trang gọi khi mở bằng trình duyệt: dữ liệu của trang SPA."""

    url: str
    method: str
    status: int
    body: bytes
    request_json: Any = None  # body JSON của request (API tìm kiếm dạng POST)


@dataclass(frozen=True, slots=True)
class RawResponse:
    status: int
    url: str  # URL cuối cùng sau redirect
    headers: Mapping[str, str]  # khoá viết thường
    content: bytes
    captured: tuple[CapturedJson, ...] = ()
    # Proxy transport tự chọn (proxy tại worker: "w:<id>"), khi backend không chỉ định proxy.
    proxy_id: str | None = None


class TransportError(Exception):
    def __init__(self, kind: TransportErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind

    @property
    def proxy_fault(self) -> bool:
        """Lỗi có khả năng do proxy (để tính vào sức khoẻ proxy)."""
        return self.kind in ("proxy", "connect")


class Transport(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        data: Any = None,
        json: Any = None,
        proxy: str | None,
        timeout: float,
        allow_redirects: bool,
    ) -> RawResponse: ...

    async def close(self) -> None: ...


class Renderer(Protocol):
    async def render(
        self,
        url: str,
        *,
        proxy: Proxy | None,
        timeout: float,
        wait_until: WaitUntil,
        capture_json: bool = False,
    ) -> RawResponse: ...


# --- kết quả -------------------------------------------------------------------------------------


def decode_body(content: bytes, content_type: str | None) -> str:
    """Giải mã theo charset trong header, rồi `<meta charset>`, mặc định UTF-8."""
    candidates: list[str] = []
    if content_type and (m := _HEADER_CHARSET.search(content_type)):
        candidates.append(m.group(1).strip("\"'"))
    if m := _META_CHARSET.search(content[:4096]):
        candidates.append(m.group(1).decode("ascii", "ignore"))
    for charset in candidates:
        try:
            return content.decode(charset)
        except (LookupError, UnicodeDecodeError):
            continue
    return content.decode("utf-8", errors="replace")


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int | None
    outcome: FetchOutcome
    headers: Mapping[str, str] = field(default_factory=dict)
    content: bytes = b""
    error: str | None = None
    source: str | None = None
    proxy_id: str | None = None
    duration_ms: int = 0
    attempts: int = 0
    from_cache: bool = False
    rendered: bool = False
    fetched_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    captured: tuple[CapturedJson, ...] = ()  # JSON bắt được khi render (`capture_json`)

    @property
    def ok(self) -> bool:
        return self.outcome is FetchOutcome.OK

    @cached_property
    def text(self) -> str:
        return decode_body(self.content, self.headers.get("content-type"))

    def json(self) -> Any:
        return jsonlib.loads(self.text)


class Fetcher(Protocol):
    async def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        data: Any = None,
        json: Any = None,
        source: str | None = None,
        rate: float | None = None,
        use_cache: bool = True,
        check_robots: bool = True,
        timeout: float | None = None,
        allow_redirects: bool = True,
        retries: int | None = None,
    ) -> FetchResult: ...

    async def render(
        self,
        url: str,
        *,
        source: str | None = None,
        rate: float | None = None,
        check_robots: bool = True,
        timeout: float | None = None,
        wait_until: WaitUntil = "domcontentloaded",
        capture_json: bool = False,
    ) -> FetchResult: ...


# --- cache ---------------------------------------------------------------------------------------


class HttpCache:
    """Cache ngắn hạn theo URL (GET 2xx) để không fetch lại một URL trong một lượt."""

    MAX_BYTES = 5 * 1024 * 1024
    _KEPT_HEADERS = ("content-type", "etag", "last-modified")

    def __init__(self, kv: KeyValueStore, *, ttl: float) -> None:
        self._kv = kv
        self._ttl = ttl

    @staticmethod
    def _key(url: str) -> str:
        return "http:" + hashlib.sha256(url.encode()).hexdigest()

    async def get(self, url: str) -> FetchResult | None:
        if self._ttl <= 0:
            return None
        blob = await self._kv.get(self._key(url))
        if blob is None:
            return None
        raw = zlib.decompress(blob)
        size = int.from_bytes(raw[:4], "big")
        meta = jsonlib.loads(raw[4 : 4 + size])
        return FetchResult(
            url=url,
            final_url=meta["final_url"],
            status=meta["status"],
            outcome=FetchOutcome.OK,
            headers=meta["headers"],
            content=raw[4 + size :],
            source=meta.get("source"),
            from_cache=True,
        )

    async def set(self, result: FetchResult) -> None:
        if self._ttl <= 0 or len(result.content) > self.MAX_BYTES:
            return
        meta = jsonlib.dumps(
            {
                "final_url": result.final_url,
                "status": result.status,
                "headers": {
                    k: result.headers[k] for k in self._KEPT_HEADERS if k in result.headers
                },
                "source": result.source,
            }
        ).encode()
        blob = zlib.compress(len(meta).to_bytes(4, "big") + meta + result.content, level=3)
        await self._kv.set(self._key(result.url), blob, ttl=self._ttl)


# --- client --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HttpConfig:
    timeout: float = 20.0
    max_retries: int = 3
    backoff_base: float = 1.0
    backoff_cap: float = 15.0
    max_retry_after: float = 30.0
    cache_ttl: float = 3600
    robots_ttl: float = 86_400
    robots_user_agents: tuple[str, ...] = ("*",)
    default_rate: float = 1.0
    burst: int = 1
    rate_overrides: Mapping[str, float] = field(default_factory=dict)
    accept_language: str = "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7"

    @classmethod
    def from_settings(cls, s: Settings) -> HttpConfig:
        return cls(
            timeout=s.http_timeout_seconds,
            max_retries=s.http_max_retries,
            backoff_base=s.http_backoff_base_seconds,
            backoff_cap=s.http_backoff_cap_seconds,
            max_retry_after=s.http_max_retry_after_seconds,
            cache_ttl=s.http_cache_ttl_seconds,
            robots_ttl=s.robots_cache_seconds,
            robots_user_agents=tuple(s.robots_user_agents),
            default_rate=s.rate_limit_per_second,
            burst=s.rate_limit_burst,
            rate_overrides=dict(s.rate_limits),
            accept_language=s.http_accept_language,
        )


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """`Retry-After` dạng số giây hoặc HTTP-date → số giây (≥ 0)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


def with_params(url: str, params: Mapping[str, Any] | None) -> str:
    if not params:
        return url
    sep = "&" if urlsplit(url).query else "?"
    return f"{url}{sep}{urlencode(params, doseq=True)}"


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


class HttpClient:
    """Cài đặt `Fetcher` dùng cho mọi nguồn.

    Thứ tự mỗi request: robots.txt → cache → (proxy → rate limit → gửi) × retry
    → nhận diện trang chặn → báo sức khoẻ proxy → cache.
    """

    def __init__(
        self,
        transport: Transport,
        *,
        kv: KeyValueStore,
        proxies: ProxyPool,
        ratelimiter: RateLimiter,
        renderer: Renderer | None = None,
        config: HttpConfig | None = None,
        sleep: Sleep = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._transport = transport
        self._proxies = proxies
        self._limiter = ratelimiter
        self._renderer = renderer
        self.config = config or HttpConfig()
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._cache = HttpCache(kv, ttl=self.config.cache_ttl)
        self.robots = RobotsCache(
            kv,
            self._fetch_robots,
            ttl=self.config.robots_ttl,
            user_agents=self.config.robots_user_agents,
        )

    @property
    def transport(self) -> Transport:
        return self._transport

    @property
    def proxies(self) -> ProxyPool:
        return self._proxies

    async def close(self) -> None:
        await self._transport.close()

    # --- tiện ích ---

    def _rate_for(self, host: str, rate: float | None, robots: RobotsRules | None) -> float:
        effective = self.config.rate_overrides.get(host) or rate or self.config.default_rate
        if robots is not None and robots.crawl_delay:
            effective = min(effective, 1 / robots.crawl_delay)
        return effective

    def _backoff(self, attempt: int) -> float:
        delay = min(self.config.backoff_cap, self.config.backoff_base * (2**attempt))
        return delay * self._rng.uniform(0.5, 1.0)

    def _headers(self, extra: Mapping[str, str] | None) -> dict[str, str]:
        headers = {"Accept-Language": self.config.accept_language}
        if extra:
            headers.update(extra)
        return headers

    async def _fetch_robots(self, url: str) -> tuple[int | None, str]:
        result = await self.fetch(url, check_robots=False, use_cache=False, source="robots")
        if result.outcome in (FetchOutcome.TIMEOUT, FetchOutcome.ERROR) and result.status is None:
            return None, ""
        if result.outcome is FetchOutcome.BLOCKED:
            return result.status or 403, ""
        return result.status, result.text

    async def _robots_check(
        self, url: str, source: str | None
    ) -> tuple[RobotsRules | None, FetchResult | None]:
        rules = await self.robots.rules_for(url)
        if rules.allowed(url):
            return rules, None
        # robots.txt không tải được cũng tạm coi như cấm, nhưng ghi rõ để không lẫn với luật cấm.
        reason = "robots_unreachable" if rules.unreachable else "robots_disallowed"
        log.info(f"source.{reason}", url=url, source=source)
        denied = FetchResult(
            url=url,
            final_url=url,
            status=None,
            outcome=FetchOutcome.ROBOTS_DISALLOWED,
            error=reason,
            source=source,
        )
        return rules, denied

    # --- Fetcher ---

    async def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        data: Any = None,
        json: Any = None,
        source: str | None = None,
        rate: float | None = None,
        use_cache: bool = True,
        check_robots: bool = True,
        timeout: float | None = None,
        allow_redirects: bool = True,
        retries: int | None = None,
    ) -> FetchResult:
        """`retries`: số lần thử lại khi lỗi tạm (mặc định HTTP_MAX_RETRIES); 0 cho URL đoán."""
        url = with_params(url, params)
        host = host_of(url)
        method = method.upper()

        robots: RobotsRules | None = None
        if check_robots:
            robots, denied = await self._robots_check(url, source)
            if denied is not None:
                return denied

        cacheable = use_cache and method == "GET" and data is None and json is None
        if cacheable and (cached := await self._cache.get(url)) is not None:
            return replace(cached, source=source or cached.source)

        result = await self._send_with_retries(
            method,
            url,
            host=host,
            headers=self._headers(headers),
            data=data,
            json=json,
            source=source,
            rate=self._rate_for(host, rate, robots),
            timeout=timeout or self.config.timeout,
            allow_redirects=allow_redirects,
            retries=self.config.max_retries if retries is None else retries,
        )
        if cacheable and result.ok and result.status is not None and result.status < 300:
            await self._cache.set(result)
        return result

    async def _send_with_retries(
        self,
        method: str,
        url: str,
        *,
        host: str,
        headers: Mapping[str, str],
        data: Any,
        json: Any,
        source: str | None,
        rate: float,
        timeout: float,
        allow_redirects: bool,
        retries: int,
    ) -> FetchResult:
        started = time.monotonic()
        last: FetchResult | None = None
        max_attempts = max(0, retries) + 1

        def result(**kw: Any) -> FetchResult:
            kw.setdefault("final_url", url)
            return FetchResult(
                url=url,
                source=source,
                duration_ms=int((time.monotonic() - started) * 1000),
                **kw,
            )

        for attempt in range(max_attempts):
            try:
                proxy = await self._proxies.acquire(host)
            except NoProxyAvailableError as exc:
                return result(
                    status=None, outcome=FetchOutcome.ERROR, error=str(exc), attempts=attempt
                )
            proxy_id = proxy.id if proxy else None
            await self._limiter.acquire(host, rate=rate, burst=self.config.burst)
            t0 = time.monotonic()
            try:
                raw = await self._transport.request(
                    method,
                    url,
                    headers=headers,
                    data=data,
                    json=json,
                    proxy=proxy.url if proxy else None,
                    timeout=timeout,
                    allow_redirects=allow_redirects,
                )
            except TransportError as exc:
                if proxy is not None and exc.proxy_fault:
                    await self._proxies.report_failure(proxy, host)
                outcome = FetchOutcome.TIMEOUT if exc.kind == "timeout" else FetchOutcome.ERROR
                last = result(
                    status=None,
                    outcome=outcome,
                    error=f"{exc.kind}: {exc}",
                    proxy_id=proxy_id,
                    attempts=attempt + 1,
                )
                log.debug(
                    "source.fetch", url=url, source=source, error=last.error, attempt=attempt + 1
                )
                if attempt + 1 < max_attempts:
                    delay = self._backoff(attempt)
                    log.info("http.retry", url=url, reason=exc.kind, delay=round(delay, 2))
                    await self._sleep(delay)
                    continue
                return last

            log.debug(
                "source.fetch",
                url=url,
                source=source,
                status=raw.status,
                proxy_id=proxy_id or raw.proxy_id,
                ms=int((time.monotonic() - t0) * 1000),
                attempt=attempt + 1,
            )
            common: dict[str, Any] = {
                "final_url": raw.url,
                "status": raw.status,
                "headers": raw.headers,
                "content": raw.content,
                "proxy_id": proxy_id or raw.proxy_id,
                "attempts": attempt + 1,
            }

            reason = detect_block(
                raw.status,
                raw.headers,
                decode_body(raw.content, raw.headers.get("content-type")),
                requested_url=url,
                final_url=raw.url,
            )
            if reason is not None:
                if proxy is not None:
                    await self._proxies.report_blocked(proxy, host)
                log.warning("source.blocked", url=url, source=source, reason=reason)
                return result(outcome=FetchOutcome.BLOCKED, error=reason, **common)

            if raw.status == 429 or raw.status in RETRY_STATUSES:
                retry_after = parse_retry_after(raw.headers.get("retry-after"))
                if raw.status == 429:
                    if proxy is not None:
                        await self._proxies.report_blocked(proxy, host)
                    await self._limiter.penalize(host, retry_after or self._backoff(attempt))
                too_long = retry_after is not None and retry_after > self.config.max_retry_after
                if attempt + 1 < max_attempts and not too_long:
                    delay = retry_after if retry_after is not None else self._backoff(attempt)
                    log.info("http.retry", url=url, status=raw.status, delay=round(delay, 2))
                    await self._sleep(delay)
                    continue
                if raw.status == 429:
                    log.warning("source.blocked", url=url, source=source, reason="rate_limited")
                    return result(outcome=FetchOutcome.BLOCKED, error="rate_limited", **common)
                return result(outcome=FetchOutcome.ERROR, error=f"http_{raw.status}", **common)

            if proxy is not None:
                await self._proxies.report_success(proxy, host)
            if raw.status >= 400:
                return result(outcome=FetchOutcome.ERROR, error=f"http_{raw.status}", **common)
            return result(outcome=FetchOutcome.OK, **common)

        assert last is not None  # vòng lặp luôn trả về trước khi tới đây
        return last

    async def render(
        self,
        url: str,
        *,
        source: str | None = None,
        rate: float | None = None,
        check_robots: bool = True,
        timeout: float | None = None,
        wait_until: WaitUntil = "domcontentloaded",
        capture_json: bool = False,
    ) -> FetchResult:
        """Mở trang bằng Chromium (tại worker; chỉ khi HTTP không đủ). Không retry: tốn tài nguyên.

        `capture_json`: giữ các phản hồi JSON trang gọi (`FetchResult.captured`)."""
        host = host_of(url)
        started = time.monotonic()

        def result(**kw: Any) -> FetchResult:
            kw.setdefault("final_url", url)
            return FetchResult(
                url=url,
                source=source,
                rendered=True,
                duration_ms=int((time.monotonic() - started) * 1000),
                **kw,
            )

        if self._renderer is None:
            return result(status=None, outcome=FetchOutcome.ERROR, error="browser_unavailable")

        robots: RobotsRules | None = None
        if check_robots:
            robots, denied = await self._robots_check(url, source)
            if denied is not None:
                denied.rendered = True
                return denied

        try:
            proxy = await self._proxies.acquire(host, browser=True)
        except NoProxyAvailableError as exc:
            return result(status=None, outcome=FetchOutcome.ERROR, error=str(exc))
        proxy_id = proxy.id if proxy else None
        await self._limiter.acquire(
            host, rate=self._rate_for(host, rate, robots), burst=self.config.burst
        )
        try:
            raw = await self._renderer.render(
                url,
                proxy=proxy,
                timeout=timeout or self.config.timeout * 2,
                wait_until=wait_until,
                capture_json=capture_json,
            )
        except TransportError as exc:
            if proxy is not None and exc.proxy_fault:
                await self._proxies.report_failure(proxy, host)
            outcome = FetchOutcome.TIMEOUT if exc.kind == "timeout" else FetchOutcome.ERROR
            return result(
                status=None, outcome=outcome, error=f"{exc.kind}: {exc}", proxy_id=proxy_id
            )

        common: dict[str, Any] = {
            "final_url": raw.url,
            "status": raw.status,
            "headers": raw.headers,
            "content": raw.content,
            "proxy_id": proxy_id or raw.proxy_id,
            "attempts": 1,
            "captured": raw.captured,
        }
        reason = detect_block(
            raw.status,
            raw.headers,
            decode_body(raw.content, raw.headers.get("content-type")),
            requested_url=url,
            final_url=raw.url,
        )
        if reason is None and raw.status == 429:
            reason = "rate_limited"
        if reason is not None:
            if proxy is not None:
                await self._proxies.report_blocked(proxy, host)
            log.warning("source.blocked", url=url, source=source, reason=reason, rendered=True)
            return result(outcome=FetchOutcome.BLOCKED, error=reason, **common)
        if proxy is not None:
            await self._proxies.report_success(proxy, host)
        if raw.status >= 400:
            return result(outcome=FetchOutcome.ERROR, error=f"http_{raw.status}", **common)
        return result(outcome=FetchOutcome.OK, **common)
