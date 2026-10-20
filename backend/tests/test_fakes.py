from __future__ import annotations

from pathlib import Path

from jobcrawler.common.schemas import FetchOutcome
from jobcrawler.services.fakes import FakeFetcher
from jobcrawler.services.net.http import Fetcher


async def _use(fetcher: Fetcher, url: str) -> str:
    """Code nguồn chỉ biết giao diện `Fetcher`."""
    result = await fetcher.fetch(url, source="test")
    return result.text if result.ok else f"lỗi {result.status}"


async def test_fake_fetcher_routes_and_records(tmp_path: Path) -> None:
    fixture = tmp_path / "jobs.json"
    fixture.write_text('{"jobs": [{"title": "Kỹ sư"}]}', encoding="utf-8")

    fetcher = FakeFetcher()
    fetcher.add("https://a.vn/careers", "<h1>Tuyển dụng</h1>")
    fetcher.add_file("https://api.ats.io/jobs?page=1", fixture)
    fetcher.add("https://a.vn/blocked", status=403, outcome=FetchOutcome.BLOCKED)

    assert await _use(fetcher, "https://a.vn/careers") == "<h1>Tuyển dụng</h1>"
    jobs = await fetcher.fetch("https://api.ats.io/jobs", params={"page": 1})
    assert jobs.json() == {"jobs": [{"title": "Kỹ sư"}]}
    assert jobs.headers["content-type"] == "application/json"
    assert (await fetcher.fetch("https://a.vn/blocked")).outcome is FetchOutcome.BLOCKED
    assert await _use(fetcher, "https://a.vn/missing") == "lỗi 404"

    rendered = await fetcher.render("https://a.vn/careers")
    assert rendered.rendered
    assert rendered.ok
    assert [(kind, url) for kind, url, _ in fetcher.calls] == [
        ("fetch", "https://a.vn/careers"),
        ("fetch", "https://api.ats.io/jobs?page=1"),
        ("fetch", "https://a.vn/blocked"),
        ("fetch", "https://a.vn/missing"),
        ("render", "https://a.vn/careers"),
    ]
