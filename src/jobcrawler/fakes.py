"""Fake cho test, không gọi mạng.

- `FakeFetcher`: thay `HttpClient` khi test nguồn/pipeline (trả nội dung fixture theo URL).
- `FakeTransport` / `FakeRenderer`: thay curl_cffi / Chromium khi test chính `HttpClient`.
- `FakeChatClient`: thay `AsyncOpenAI` khi test `LLMClient` và các bước dùng LLM.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobcrawler.net.http import FetchResult, RawResponse, TransportError, WaitUntil, with_params
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
        self, url: str, *, proxy: Proxy | None, timeout: float, wait_until: WaitUntil
    ) -> RawResponse:
        self.calls.append((url, proxy))
        reply = self.routes.get(url) or response(url, "not found", status=404)
        if isinstance(reply, TransportError):
            raise reply
        return reply


@dataclass
class FakeFetcher:
    """Cài đặt `Fetcher` trả kết quả dựng sẵn theo URL; ghi lại mọi lời gọi."""

    results: dict[str, FetchResult] = field(default_factory=dict)
    calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)

    def add(
        self,
        url: str,
        body: str | bytes = b"",
        *,
        status: int = 200,
        content_type: str = "text/html; charset=utf-8",
        headers: Mapping[str, str] | None = None,
        outcome: FetchOutcome | None = None,
        final_url: str | None = None,
    ) -> None:
        content = body.encode() if isinstance(body, str) else body
        if outcome is None:
            outcome = FetchOutcome.OK if status < 400 else FetchOutcome.ERROR
        self.results[url] = FetchResult(
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

    def add_file(self, url: str, path: str | Path, **kwargs: Any) -> None:
        """Dùng file fixture (tests/fixtures/<source>/...) làm nội dung của URL."""
        path = Path(path)
        if "content_type" not in kwargs and path.suffix == ".json":
            kwargs["content_type"] = "application/json"
        self.add(url, path.read_bytes(), **kwargs)

    def _get(self, url: str, kind: str, kwargs: dict[str, Any]) -> FetchResult:
        self.calls.append((kind, url, kwargs))
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
        )

    async def fetch(self, url: str, **kwargs: Any) -> FetchResult:
        return self._get(with_params(url, kwargs.get("params")), "fetch", kwargs)

    async def render(self, url: str, **kwargs: Any) -> FetchResult:
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
