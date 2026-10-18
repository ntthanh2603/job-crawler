"""Fake cho test, không gọi mạng.

- `FakeFetcher`: thay `HttpClient` khi test nguồn/pipeline (trả nội dung fixture theo URL).
- `FakeTransport` / `FakeRenderer`: thay curl_cffi / Chromium khi test chính `HttpClient`.
- `FakeChatClient`: thay `AsyncOpenAI` khi test `LLMClient` và các bước dùng LLM.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from jobcrawler.net.http import (
    CapturedJson,
    FetchResult,
    RawResponse,
    TransportError,
    WaitUntil,
    with_params,
)
from jobcrawler.net.proxy_pool import Proxy
from jobcrawler.schemas import FetchOutcome

Reply = RawResponse | TransportError | Callable[..., RawResponse]


def response(
    url: str,
    body: str | bytes = b"",
    *,
    status: int = 200,
    headers: Mapping[str, str] | None = None,
    final_url: str | None = None,
    content_type: str = "text/html; charset=utf-8",
) -> RawResponse:
    content = body.encode() if isinstance(body, str) else body
    merged = {"content-type": content_type, **{k.lower(): v for k, v in (headers or {}).items()}}
    return RawResponse(status=status, url=final_url or url, headers=merged, content=content)


@dataclass(slots=True)
class Call:
    method: str
    url: str
    headers: Mapping[str, str]
    proxy: str | None
    data: Any = None
    json: Any = None


class FakeTransport:
    """Trả lời theo URL. Một danh sách được dùng lần lượt (lần cuối lặp lại), hợp cho retry.

    URL không đăng ký trả 404 (kể cả robots.txt → được crawl toàn bộ).
    """

    def __init__(self, routes: Mapping[str, Reply | Sequence[Reply]] | None = None) -> None:
        self._routes: dict[str, list[Reply]] = {}
        self.calls: list[Call] = []
        self.closed = False
        for url, reply in (routes or {}).items():
            self.add(url, reply)

    def add(self, url: str, reply: Reply | Sequence[Reply]) -> None:
        if isinstance(reply, (RawResponse, TransportError)) or callable(reply):
            self._routes[url] = [reply]  # pyright: ignore[reportArgumentType]
        else:
            self._routes[url] = list(reply)

    def urls(self) -> list[str]:
        return [c.url for c in self.calls]

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
    ) -> RawResponse:
        self.calls.append(Call(method, url, dict(headers), proxy, data, json))
        replies = self._routes.get(url)
        if not replies:
            return response(url, "not found", status=404, content_type="text/plain")
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        if isinstance(reply, TransportError):
            raise reply
        if isinstance(reply, RawResponse):
            return reply
        return reply(method=method, url=url, headers=headers, proxy=proxy)

    async def close(self) -> None:
        self.closed = True


class FakeRenderer:
    def __init__(self, routes: Mapping[str, RawResponse | TransportError] | None = None) -> None:
        self.routes = dict(routes or {})
        self.calls: list[tuple[str, Proxy | None]] = []

    async def render(
        self,
        url: str,
        *,
        proxy: Proxy | None,
        timeout: float,
        wait_until: WaitUntil,
        capture_json: bool = False,
    ) -> RawResponse:
        self.calls.append((url, proxy))
        reply = self.routes.get(url) or response(url, "not found", status=404)
        if isinstance(reply, TransportError):
            raise reply
        return reply


def fetch_result(
    url: str,
    body: str | bytes = b"",
    *,
    status: int = 200,
    content_type: str = "text/html; charset=utf-8",
    headers: Mapping[str, str] | None = None,
    outcome: FetchOutcome | None = None,
    final_url: str | None = None,
) -> FetchResult:
    content = body.encode() if isinstance(body, str) else body
    if outcome is None:
        outcome = FetchOutcome.OK if status < 400 else FetchOutcome.ERROR
    return FetchResult(
        url=url,
        final_url=final_url or url,
        status=status,
        outcome=outcome,
        headers={
            "content-type": content_type,
            **{k.lower(): v for k, v in (headers or {}).items()},
        },
        content=content,
        error=None if outcome is FetchOutcome.OK else f"http_{status}",
    )


def _file_kwargs(path: Path, kwargs: dict[str, Any]) -> dict[str, Any]:
    if "content_type" not in kwargs and path.suffix == ".json":
        kwargs["content_type"] = "application/json"
    if "content_type" not in kwargs and path.suffix in (".rss", ".xml"):
        kwargs["content_type"] = "application/rss+xml; charset=utf-8"
    return kwargs


@dataclass
class FakeFetcher:
    """Cài đặt `Fetcher` trả kết quả dựng sẵn theo URL; ghi lại mọi lời gọi.

    `render()` dùng `rendered` nếu URL có ở đó (DOM sau khi chạy JavaScript), không thì như
    `fetch()`. `add_route()` cho câu trả lời phụ thuộc request (vd. body JSON của POST).
    """

    results: dict[str, FetchResult] = field(default_factory=dict)
    rendered: dict[str, FetchResult] = field(default_factory=dict)
    routes: dict[str, Callable[[dict[str, Any]], FetchResult]] = field(default_factory=dict)
    calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    delay: float = 0.0  # giây chờ mỗi lời gọi (thử deadline)

    def add(self, url: str, body: str | bytes = b"", **kwargs: Any) -> None:
        self.results[url] = fetch_result(url, body, **kwargs)

    def add_file(self, url: str, path: str | Path, **kwargs: Any) -> None:
        """Dùng file fixture (tests/fixtures/<source>/...) làm nội dung của URL."""
        path = Path(path)
        self.add(url, path.read_bytes(), **_file_kwargs(path, kwargs))

    def add_rendered_file(self, url: str, path: str | Path, **kwargs: Any) -> None:
        path = Path(path)
        self.rendered[url] = fetch_result(url, path.read_bytes(), **_file_kwargs(path, kwargs))

    def add_rendered(
        self,
        url: str,
        body: str | bytes = b"",
        *,
        captured: Sequence[CapturedJson] = (),
        **kwargs: Any,
    ) -> None:
        """DOM sau khi chạy JavaScript, kèm phản hồi JSON trang gọi (khi `capture_json`)."""
        result = fetch_result(url, body, **kwargs)
        self.rendered[url] = replace(result, captured=tuple(captured))

    def add_route(self, url: str, handler: Callable[[dict[str, Any]], FetchResult]) -> None:
        """`handler(kwargs của fetch)` → kết quả."""
        self.routes[url] = handler

    def _get(self, url: str, kind: str, kwargs: dict[str, Any]) -> FetchResult:
        self.calls.append((kind, url, kwargs))
        found: FetchResult | None = None
        if kind == "render":
            found = self.rendered.get(url)
        if found is None and url in self.routes:
            found = self.routes[url](kwargs)
        if found is None:
            found = self.results.get(url)
        if found is None:
            return FetchResult(
                url=url,
                final_url=url,
                status=404,
                outcome=FetchOutcome.ERROR,
                error="http_404",
                source=kwargs.get("source"),
            )
        return FetchResult(
            url=found.url,
            final_url=found.final_url,
            status=found.status,
            outcome=found.outcome,
            headers=found.headers,
            content=found.content,
            error=found.error,
            source=kwargs.get("source"),
            rendered=kind == "render",
            captured=found.captured if kwargs.get("capture_json") else (),
        )

    async def fetch(self, url: str, **kwargs: Any) -> FetchResult:
        if self.delay:
            await asyncio.sleep(self.delay)
        return self._get(with_params(url, kwargs.get("params")), "fetch", kwargs)

    async def render(self, url: str, **kwargs: Any) -> FetchResult:
        if self.delay:
            await asyncio.sleep(self.delay)
        return self._get(url, "render", kwargs)


# --- LLM -----------------------------------------------------------------------------------------


@dataclass
class _Usage:
    prompt_tokens: int
    completion_tokens: int


@dataclass
class _Message:
    content: str | None


@dataclass
class _Choice:
    message: _Message


@dataclass
class _Completion:
    choices: list[_Choice]
    usage: _Usage


class FakeChatClient:
    """Giả `AsyncOpenAI` cho `LLMClient`: trả lần lượt các phản hồi dựng sẵn.

    Phần tử là chuỗi (nội dung trả về) hoặc Exception (ném ra). Ghi lại kwargs mỗi lời gọi.
    """

    def __init__(self, replies: Sequence[str | BaseException], *, delay: float = 0.0) -> None:
        self._replies = list(replies)
        self._delay = delay
        self.calls: list[dict[str, Any]] = []
        self.active = 0
        self.max_active = 0
        self.closed = False
        self.chat = self
        self.completions = self

    async def create(self, **kwargs: Any) -> _Completion:
        self.calls.append(kwargs)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            reply = self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]
            if isinstance(reply, BaseException):
                raise reply
            return _Completion([_Choice(_Message(reply))], _Usage(10, len(reply)))
        finally:
            self.active -= 1

    async def close(self) -> None:
        self.closed = True
