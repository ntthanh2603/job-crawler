from __future__ import annotations

import json
from typing import Any

from jobcrawler.common.schemas import FetchOutcome
from jobcrawler.services.fakes import FakeFetcher, fetch_result
from jobcrawler.services.net.http import FetchResult
from jobcrawler.services.net.kv import MemoryKV
from jobcrawler.services.serp import (
    BRAVE_URL,
    DISABLED,
    SERPER_URL,
    SerpClient,
    parse_brave,
    parse_serper,
)
from jobcrawler.sources.base import FetchRecord

# Dạng phản hồi của Serper (google.serper.dev/search), rút gọn.
SERPER = {
    "searchParameters": {"q": "Vinamilk", "gl": "vn", "hl": "vi", "type": "search"},
    "knowledgeGraph": {
        "title": "Vinamilk",
        "type": "Công ty sữa",
        "website": "https://www.vinamilk.com.vn/",
        "description": "Công ty Cổ phần Sữa Việt Nam, tên khác: <b>Vinamilk</b>",
        "descriptionLink": "https://vi.wikipedia.org/wiki/Vinamilk",
        "attributes": {"Trụ sở chính": "Quận 7, TP.HCM", "Ngày thành lập": ""},
    },
    "organic": [
        {
            "title": "Vinamilk - Thương hiệu sữa hàng đầu Việt Nam",
            "link": "https://www.vinamilk.com.vn/?utm_source=x",
            "snippet": "Vinamilk  cung cấp sữa...",
            "position": 1,
        },
        {"title": "Không có link", "snippet": "bỏ qua", "position": 2},
        {
            "title": "0300588569 - CÔNG TY CỔ PHẦN SỮA VIỆT NAM",
            "link": "https://masothue.com/0300588569-cong-ty-co-phan-sua-viet-nam",
            "snippet": "Mã số thuế: 0300588569",
            "position": 3,
        },
    ],
    "credits": 1,
}

BRAVE = {
    "type": "search",
    "web": {
        "results": [
            {
                "title": "Tiki - Mua hàng online",
                "url": "https://tiki.vn/",
                "description": "<strong>Tiki</strong> mua sắm",
                "extra_snippets": ["Công ty TNHH Ti Ki"],
            }
        ]
    },
    "infobox": {
        "results": [
            {
                "title": "Tiki",
                "category": "company",
                "website_url": "https://tiki.vn/",
                "long_desc": "Sàn TMĐT",
                "attributes": [["Trụ sở", "TP.HCM"], ["Bỏ", None]],
                "url": "https://en.wikipedia.org/wiki/Tiki",
            }
        ]
    },
}


def test_parse_serper() -> None:
    resp = parse_serper("Vinamilk", SERPER)
    assert [i.rank for i in resp.items] == [1, 2]
    first = resp.items[0]
    assert first.url == "https://www.vinamilk.com.vn/"  # bỏ tham số tracking
    assert first.snippet == "Vinamilk cung cấp sữa..."
    assert resp.panel is not None
    assert resp.panel.website == "https://www.vinamilk.com.vn/"
    assert resp.panel.description == "Công ty Cổ phần Sữa Việt Nam, tên khác: Vinamilk"
    assert resp.panel.attributes == {"Trụ sở chính": "Quận 7, TP.HCM"}
    assert resp.panel.source_url == "https://vi.wikipedia.org/wiki/Vinamilk"


def test_parse_brave() -> None:
    resp = parse_brave("Tiki", BRAVE)
    [item] = resp.items
    assert item.snippet == "Tiki mua sắm … Công ty TNHH Ti Ki"
    assert resp.panel is not None
    assert (resp.panel.website, resp.panel.kind) == ("https://tiki.vn/", "company")
    assert resp.panel.attributes == {"Trụ sở": "TP.HCM"}


async def test_disabled_without_key() -> None:
    fetcher = FakeFetcher()
    client = SerpClient(fetcher, api_key=None)
    resp = await client.search("Vinamilk")
    assert not client.enabled
    assert resp.error == DISABLED
    assert fetcher.calls == []


async def test_serper_request_records_and_cache() -> None:
    seen: list[dict[str, Any]] = []

    def handler(kwargs: dict[str, Any]) -> FetchResult:
        seen.append(kwargs)
        return fetch_result(SERPER_URL, json.dumps(SERPER), content_type="application/json")

    fetcher = FakeFetcher()
    fetcher.add_route(SERPER_URL, handler)
    kv = MemoryKV()
    client = SerpClient(fetcher, api_key="k", kv=kv, results=10)
    records: list[FetchRecord] = []
    resp = await client.search("  Vinamilk ", records=records)

    assert resp.ok
    assert not resp.from_cache
    [call] = seen
    assert call["method"] == "POST"
    assert call["headers"]["X-API-KEY"] == "k"
    assert call["json"] == {"q": "Vinamilk", "gl": "vn", "hl": "vi", "num": 10}
    assert call["check_robots"] is False
    assert call["use_cache"] is False
    [record] = records
    assert record.source == "serp:serper"
    assert record.url == f"{SERPER_URL}?q=Vinamilk"
    assert record.items == 2

    again = await client.search("vinamilk")  # cùng truy vấn (không phân biệt hoa thường)
    assert again.from_cache
    assert [i.url for i in again.items] == [i.url for i in resp.items]
    assert len(seen) == 1


async def test_brave_request() -> None:
    fetcher = FakeFetcher()
    url = f"{BRAVE_URL}?q=Tiki&count=5"
    fetcher.add(url, json.dumps(BRAVE), content_type="application/json")
    client = SerpClient(fetcher, provider="brave", api_key="b", results=5)
    resp = await client.search("Tiki")
    assert resp.ok
    assert resp.panel is not None
    [(kind, called, kwargs)] = fetcher.calls
    assert (kind, called) == ("fetch", url)
    assert kwargs["headers"]["X-Subscription-Token"] == "b"


async def test_errors_are_not_cached_or_raised() -> None:
    fetcher = FakeFetcher()
    fetcher.add(SERPER_URL, '{"message": "Unauthorized"}', status=403, outcome=FetchOutcome.BLOCKED)
    kv = MemoryKV()
    client = SerpClient(fetcher, api_key="bad", kv=kv)
    first = await client.search("Vinamilk")
    assert first.error is not None
    assert "SERP_API_KEY" in first.error
    assert first.items == []
    await client.search("Vinamilk")
    assert len(fetcher.calls) == 2  # lỗi không được cache

    broken = FakeFetcher()
    broken.add(SERPER_URL, "<html>not json</html>")
    resp = await SerpClient(broken, api_key="k").search("x")
    assert resp.error is not None
    assert resp.error.startswith("parse_error")
