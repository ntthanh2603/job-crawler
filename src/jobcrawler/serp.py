"""Client SERP (Serper hoặc Brave) cho KYC: tìm website chính thức và trang tra cứu MST.

Gọi qua `Fetcher` (rate limit, retry; không kiểm robots.txt vì đây là API trả phí có key).
Kết quả cache trong KV (mặc định 30 ngày) để chạy lại không tốn credit. Thiếu `SERP_API_KEY`
thì `enabled=False` và `search()` trả kết quả rỗng với `error="serp_disabled"`, không ném lỗi.
"""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlencode

from pydantic import BaseModel, Field

from jobcrawler.logging import get_logger
from jobcrawler.net.http import Fetcher
from jobcrawler.net.kv import KeyValueStore
from jobcrawler.net.urls import normalize_url
from jobcrawler.sources.base import FetchRecord

if TYPE_CHECKING:
    from jobcrawler.config import Settings

log = get_logger(__name__)

Provider = Literal["serper", "brave"]
DISABLED = "serp_disabled"
CACHE_VERSION = 1
SERPER_URL = "https://google.serper.dev/search"
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
_TAGS = re.compile(r"<[^>]+>")


class SerpItem(BaseModel):
    rank: int  # 1 = kết quả đầu tiên
    title: str
    url: str
    snippet: str = ""


class SerpPanel(BaseModel):
    """Knowledge Graph (Serper) / infobox (Brave): thực thể mà công cụ tìm kiếm nhận ra."""

    title: str
    kind: str | None = None
    website: str | None = None
    description: str | None = None
    attributes: dict[str, str] = Field(default_factory=dict)
    source_url: str | None = None


class SerpResponse(BaseModel):
    provider: str
    query: str
    items: list[SerpItem] = Field(default_factory=list)
    panel: SerpPanel | None = None
    error: str | None = None
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None


def _clean(value: Any) -> str:
    text = _TAGS.sub("", str(value or ""))
    return " ".join(text.split())


def _url(value: Any) -> str | None:
    return normalize_url(str(value)) if value else None


def parse_serper(query: str, data: dict[str, Any]) -> SerpResponse:
    items: list[SerpItem] = []
    for raw in data.get("organic") or []:
        url = _url(raw.get("link"))
        if url is None:
            continue
        items.append(
            SerpItem(
                rank=len(items) + 1,
                title=_clean(raw.get("title")),
                url=url,
                snippet=_clean(raw.get("snippet")),
            )
        )
    panel = None
    kg = data.get("knowledgeGraph")
    if isinstance(kg, dict) and kg.get("title"):
        attributes = kg.get("attributes") or {}
        panel = SerpPanel(
            title=_clean(kg["title"]),
            kind=_clean(kg.get("type")) or None,
            website=_url(kg.get("website")),
            description=_clean(kg.get("description")) or None,
            attributes={_clean(k): _clean(v) for k, v in attributes.items() if v},
            source_url=_url(kg.get("descriptionLink")),
        )
    return SerpResponse(provider="serper", query=query, items=items, panel=panel)


def parse_brave(query: str, data: dict[str, Any]) -> SerpResponse:
    items: list[SerpItem] = []
    for raw in (data.get("web") or {}).get("results") or []:
        url = _url(raw.get("url"))
        if url is None:
            continue
        snippets = [raw.get("description"), *(raw.get("extra_snippets") or [])]
        items.append(
            SerpItem(
                rank=len(items) + 1,
                title=_clean(raw.get("title")),
                url=url,
                snippet=" … ".join(s for s in (_clean(x) for x in snippets) if s),
            )
        )
    panel = None
    boxes = (data.get("infobox") or {}).get("results") or []
    if boxes and isinstance(boxes[0], dict) and boxes[0].get("title"):
        box = boxes[0]
        attributes: dict[str, str] = {}
        for pair in box.get("attributes") or []:
            if isinstance(pair, list) and len(pair) == 2 and pair[1]:
                attributes[_clean(pair[0])] = _clean(pair[1])
        panel = SerpPanel(
            title=_clean(box["title"]),
            kind=_clean(box.get("category")) or None,
            website=_url(box.get("website_url")),
            description=_clean(box.get("long_desc") or box.get("description")) or None,
            attributes=attributes,
            source_url=_url(box.get("url")),
        )
    return SerpResponse(provider="brave", query=query, items=items, panel=panel)


class SerpClient:
    def __init__(
        self,
        fetcher: Fetcher | None,
        *,
        provider: Provider = "serper",
        api_key: str | None = None,
        kv: KeyValueStore | None = None,
        cache_ttl: float = 30 * 86_400,
        rate: float = 5.0,
        results: int = 10,
        timeout: float = 15.0,
        country: str = "vn",
        language: str = "vi",
    ) -> None:
        self._fetcher = fetcher
        self.provider: Provider = provider
        self._api_key = api_key
        self._kv = kv
        self._cache_ttl = cache_ttl
        self._rate = rate
        self._results = results
        self._timeout = timeout
        self._country = country
        self._language = language

    @classmethod
    def from_settings(
        cls, settings: Settings, fetcher: Fetcher | None, kv: KeyValueStore | None
    ) -> SerpClient:
        key = settings.serp_api_key.get_secret_value() if settings.serp_api_key else None
        return cls(
            fetcher,
            provider=settings.serp_provider,
            api_key=key,
            kv=kv,
            cache_ttl=settings.serp_cache_seconds,
            rate=settings.serp_rate_per_second,
            results=settings.serp_results,
            timeout=settings.serp_timeout_seconds,
        )

    @property
    def enabled(self) -> bool:
        return bool(self._api_key) and self._fetcher is not None

    @property
    def source(self) -> str:
        return f"serp:{self.provider}"

    def _cache_key(self, query: str) -> str:
        norm = " ".join(query.lower().split())
        digest = hashlib.sha1(
            f"{self.provider}|{self._country}|{self._language}|{self._results}|{norm}".encode()
        ).hexdigest()
        return f"serp:v{CACHE_VERSION}:{digest}"

    def _request(self, query: str) -> tuple[str, dict[str, Any]]:
        assert self._api_key is not None
        if self.provider == "serper":
            return SERPER_URL, {
                "method": "POST",
                "headers": {"X-API-KEY": self._api_key, "Content-Type": "application/json"},
                "json": {
                    "q": query,
                    "gl": self._country,
                    "hl": self._language,
                    "num": self._results,
                },
            }
        # Brave không nhận mọi mã quốc gia/ngôn ngữ (sai thì trả 422): chỉ gửi truy vấn.
        url = f"{BRAVE_URL}?{urlencode({'q': query, 'count': self._results})}"
        return url, {
            "headers": {"X-Subscription-Token": self._api_key, "Accept": "application/json"}
        }

    async def search(self, query: str, *, records: list[FetchRecord] | None = None) -> SerpResponse:
        """Tìm `query`. Lỗi mạng/API → `error` (không ném); kết quả tốt được cache."""
        query = " ".join(query.split())
        if not self.enabled or self._fetcher is None:
            return SerpResponse(provider=self.provider, query=query, error=DISABLED)
        key = self._cache_key(query)
        if self._kv is not None and (blob := await self._kv.get(key)) is not None:
            try:
                cached = SerpResponse.model_validate_json(blob)
            except ValueError:
                cached = None
            if cached is not None:
                cached.from_cache = True
                log.info("serp.search", provider=self.provider, query=query, cached=True)
                return cached

        url, kwargs = self._request(query)
        result = await self._fetcher.fetch(
            url,
            source=self.source,
            rate=self._rate,
            use_cache=False,
            check_robots=False,
            timeout=self._timeout,
            **kwargs,
        )
        record = FetchRecord.from_result(self.source, result)
        # Truy vấn nằm trong body (Serper): ghi vào URL của source_fetches để truy vết.
        record.url = f"{url.split('?', 1)[0]}?{urlencode({'q': query})}"
        if records is not None:
            records.append(record)
        response = SerpResponse(provider=self.provider, query=query)
        if result.status in (401, 402, 403):
            response.error = f"http_{result.status}: SERP_API_KEY sai hoặc hết credit"
        elif not result.ok:
            response.error = result.error or result.outcome.value
        else:
            try:
                data = result.json()
                parse = parse_serper if self.provider == "serper" else parse_brave
                response = parse(query, data if isinstance(data, dict) else {})
            except ValueError as exc:
                response.error = f"parse_error: {exc}"
        record.items = len(response.items)
        if response.ok and self._kv is not None and self._cache_ttl > 0:
            payload = response.model_dump_json(exclude={"from_cache"})
            await self._kv.set(key, payload, ttl=self._cache_ttl)
        log_fn = log.info if response.ok else log.warning
        log_fn(
            "serp.search",
            provider=self.provider,
            query=query,
            cached=False,
            items=len(response.items),
            panel=response.panel.website if response.panel else None,
            error=response.error,
        )
        return response
