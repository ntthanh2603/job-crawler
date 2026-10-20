from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from jobcrawler.common.schemas import FetchOutcome
from jobcrawler.pipeline.discovery import (
    brand_careers_site,
    careers_score,
    detail_parent,
    discover,
    sources_for,
)
from jobcrawler.services.fakes import FakeChatClient, FakeFetcher
from jobcrawler.services.llm import LLMClient
from jobcrawler.services.net.http import FetchResult
from jobcrawler.services.net.kv import MemoryKV
from jobcrawler.sources.ats.greenhouse import GreenhouseSource
from jobcrawler.sources.base import SourceSession
from jobcrawler.sources.careers import CareersSource

FIXTURES = Path(__file__).parent / "fixtures" / "careers"
LISTING = (
    '<html><body><h1>Tuyển dụng</h1><a href="/tuyen-dung/ke-toan-tong-hop-12">Kế toán tổng hợp'
    '</a><a href="/tuyen-dung/nhan-vien-kinh-doanh-13">Nhân viên kinh doanh</a></body></html>'
)


def html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def session(fetcher: FakeFetcher, llm: LLMClient | None = None) -> SourceSession:
    return SourceSession(
        "discovery:test", fetcher, llm or LLMClient(FakeChatClient(["{}"]), model=None)
    )


def test_careers_score() -> None:
    site = "https://www.momo.vn/"
    assert careers_score("https://www.momo.vn/tuyen-dung", "Tuyển dụng", site) == 5
    assert careers_score("https://tuyendung.momo.vn/", "", site) == 2
    assert careers_score("https://www.momo.vn/tin-tuc/tuyen-dung-2026", "Tuyển dụng", site) == 0
    assert careers_score("https://www.topcv.vn/cong-ty/momo", "Tuyển dụng", site) == 0
    assert careers_score("https://www.momo.vn/vi-tra-sau", "Ví trả sau", site) == 0
    assert brand_careers_site("https://momo.careers/", site)
    assert brand_careers_site(
        "https://haravan-careers.myharavan.com/pages/x", "https://haravan.com/"
    )
    assert not brand_careers_site("https://momo.vn/careers", site)  # cùng site
    assert not brand_careers_site("https://careers.other.vn/", site)


async def test_base_vn_from_homepage_and_cache() -> None:
    fetcher = FakeFetcher()
    fetcher.add("https://base.vn/", html("base_vn/home.html"))
    fetcher.add("https://careers.base.vn/", html("base_vn/careers.html"))
    kv = MemoryKV()

    result = await discover(session(fetcher), kv, "base.vn")
    assert result.careers_pages == ["https://careers.base.vn/"]
    assert result.method == "homepage"
    assert result.homepage == "https://base.vn/"
    assert not result.from_cache
    assert [s.name for s in sources_for(result)] == ["careers:careers.base.vn"]

    calls = len(fetcher.calls)
    cached = await discover(session(fetcher), kv, "https://www.base.vn/")
    assert cached.from_cache
    assert cached.careers_pages == result.careers_pages
    assert len(fetcher.calls) == calls  # không gọi mạng lần nữa

    refreshed = await discover(session(fetcher), kv, "base.vn", refresh=True)
    assert not refreshed.from_cache
    assert len(fetcher.calls) > calls


def test_detail_parent() -> None:
    assert detail_parent("https://acme.vn/tuyen-dung/ke-toan-tong-hop-12") == (
        "https://acme.vn/tuyen-dung"
    )
    assert detail_parent("https://careers.base.vn/job/hn-sales-29675?ref=home") == (
        "https://careers.base.vn/job"
    )
    assert (
        detail_parent("https://acme.vn/vi/tuyen-dung/123.html") == "https://acme.vn/vi/tuyen-dung"
    )
    assert detail_parent("https://acme.vn/tuyen-dung") is None  # chính là trang danh sách
    assert detail_parent("https://acme.vn/jobs?office=3") is None
    assert detail_parent("https://acme.vn/tuyen-dung/ha-noi") is None  # không có id: trang con
    assert detail_parent("https://acme.vn/tin-tuc/bai-viet-12") is None  # không dưới đoạn careers


async def test_careers_site_without_llm_skips_job_pages() -> None:
    """Trang careers chỉ có link tới từng tin (cùng điểm với link danh sách): không có LLM chọn
    thì không nhận một trang tin làm nguồn, mà thử trang danh sách cha rồi chính trang careers."""
    job = (
        "https://careers.base.vn/job/hn-business-development-executive-junior-b2b-sales-1yoe-29675"
    )
    fetcher = FakeFetcher()
    fetcher.add("https://careers.base.vn/", html("base_vn/careers.html"))
    fetcher.add(job, html("base_vn/job_29675.html"))
    result = await discover(session(fetcher), MemoryKV(), "https://careers.base.vn/")
    assert result.careers_pages == ["https://careers.base.vn/"]
    assert job not in [url for _, url, _ in fetcher.calls]


async def test_momo_brand_domain_hops_and_render() -> None:
    """momo.vn → momo.careers (domain thương hiệu) → /jobs (danh sách nạp bằng JavaScript)."""
    fetcher = FakeFetcher()
    fetcher.add("https://momo.vn/", html("momo/home.html"), final_url="https://www.momo.vn/")
    fetcher.add(
        "https://momo.careers/",
        '<html><body><h1>MoMo Careers</h1><a href="/jobs">Cơ hội việc làm</a>'
        + "<p>"
        + "Gia nhập MoMo. " * 200
        + "</p></body></html>",
    )
    fetcher.add("https://momo.careers/jobs", html("momo/careers_jobs.html"))
    fetcher.add_rendered_file(
        "https://momo.careers/jobs", FIXTURES / "momo/careers_jobs_rendered.html"
    )

    result = await discover(session(fetcher), MemoryKV(), "momo.vn")
    assert result.homepage == "https://www.momo.vn/"
    assert result.careers_pages == ["https://momo.careers/jobs"]
    assert any("(trình duyệt)" in step for step in result.steps)


async def test_ats_on_homepage() -> None:
    fetcher = FakeFetcher()
    fetcher.add(
        "https://acme.vn/",
        '<a href="https://job-boards.greenhouse.io/acme">Careers</a>'
        '<a href="https://www.facebook.com/acme">Facebook</a>',
    )
    result = await discover(session(fetcher), MemoryKV(), "acme.vn")
    assert [r.name for r in result.ats] == ["greenhouse:acme"]
    assert result.careers_pages == []
    [source] = sources_for(result)
    assert isinstance(source, GreenhouseSource)


async def test_input_ats_url_needs_no_fetch() -> None:
    fetcher = FakeFetcher()
    kv = MemoryKV()
    result = await discover(session(fetcher), kv, "https://jobs.lever.co/acme/123")
    assert [r.name for r in result.ats] == ["lever:acme"]
    assert result.method == "input_ats"
    assert fetcher.calls == []
    # Bảng khác trên cùng host ATS: không dùng nhầm kết quả của bảng trước.
    other = await discover(session(fetcher), kv, "https://jobs.lever.co/other")
    assert [r.name for r in other.ats] == ["lever:other"]
    assert not other.from_cache


def test_cache_id() -> None:
    from jobcrawler.pipeline.discovery import cache_id

    assert cache_id("https://www.base.vn/") == "base.vn"
    assert cache_id("base.vn") == "base.vn"
    assert cache_id("https://acme.vn/ve-chung-toi/tuyen-dung/") == "acme.vn/ve-chung-toi/tuyen-dung"
    assert cache_id("https://jobs.lever.co/acme") == "jobs.lever.co/acme"


async def test_input_url_with_path_is_tried_first() -> None:
    fetcher = FakeFetcher()
    fetcher.add("https://acme.vn/", "<html><body>Trang chủ</body></html>")
    fetcher.add("https://acme.vn/ve-chung-toi/tuyen-dung", LISTING)
    result = await discover(session(fetcher), MemoryKV(), "acme.vn/ve-chung-toi/tuyen-dung")
    assert result.careers_pages == ["https://acme.vn/ve-chung-toi/tuyen-dung"]


async def test_sitemap_then_common_paths() -> None:
    sitemap_index = (
        '<?xml version="1.0"?><sitemapindex><sitemap><loc>https://acme.vn/post-sitemap.xml</loc>'
        "</sitemap><sitemap><loc>https://acme.vn/page-sitemap.xml</loc></sitemap></sitemapindex>"
    )
    pages = (
        "<urlset><url><loc>https://acme.vn/gioi-thieu</loc></url>"
        "<url><loc>https://acme.vn/tuyen-dung/ke-toan-tong-hop-12</loc></url></urlset>"
    )
    fetcher = FakeFetcher()
    fetcher.add("https://acme.vn/", "<html><body>Trang chủ</body></html>")
    fetcher.add("https://acme.vn/sitemap.xml", sitemap_index, content_type="application/xml")
    fetcher.add("https://acme.vn/page-sitemap.xml", pages, content_type="application/xml")
    fetcher.add("https://acme.vn/tuyen-dung", LISTING)
    result = await discover(session(fetcher), MemoryKV(), "acme.vn")
    assert result.method == "sitemap"
    assert result.careers_pages == ["https://acme.vn/tuyen-dung"]

    fetcher = FakeFetcher()
    fetcher.add("https://acme.vn/", "<html><body>Trang chủ</body></html>")
    fetcher.add("https://acme.vn/jobs", LISTING)
    # Đường dẫn không tồn tại bị chuyển về trang chủ (soft 404): không tính.
    fetcher.add("https://acme.vn/careers", "<html>Trang chủ</html>", final_url="https://acme.vn/")
    result = await discover(session(fetcher), MemoryKV(), "acme.vn")
    assert result.method == "common_path"
    assert result.careers_pages == ["https://acme.vn/jobs"]


async def test_llm_picks_among_candidates() -> None:
    fetcher = FakeFetcher()
    fetcher.add(
        "https://acme.vn/",
        '<a href="/ve-chung-toi/van-hoa-tuyen-dung">Văn hoá tuyển dụng</a>'
        '<a href="/careers">Careers</a>',
    )
    fetcher.add("https://acme.vn/careers", LISTING)
    # Theo điểm: 0 = /careers, 1 = /ve-chung-toi/van-hoa-tuyen-dung. LLM chọn 0 (và một số
    # ngoài danh sách, bị bỏ) → trang 1 không được mở.
    chat = FakeChatClient([json.dumps({"choices": [0, 7], "reason": "trang danh sách"})])
    result = await discover(
        session(fetcher, LLMClient(chat, model="qwen-test")), MemoryKV(), "acme.vn"
    )
    assert result.careers_pages == ["https://acme.vn/careers"]
    assert result.method == "homepage"
    prompt: Any = chat.calls[0]["messages"][1]["content"]
    assert "0. Careers — https://acme.vn/careers" in prompt
    opened = [url for _, url, _ in fetcher.calls]
    assert "https://acme.vn/ve-chung-toi/van-hoa-tuyen-dung" not in opened


async def test_not_found_and_blocked() -> None:
    kv = MemoryKV()
    fetcher = FakeFetcher()
    fetcher.add("https://acme.vn/", "<html><body>Không có gì</body></html>")
    result = await discover(session(fetcher), kv, "acme.vn")
    assert not result.found
    assert result.error is None
    assert (await discover(session(fetcher), kv, "acme.vn")).from_cache  # cache ngắn hạn

    blocked = FakeFetcher()
    blocked.add("https://shop.vn/", status=403, outcome=FetchOutcome.BLOCKED)
    result = await discover(session(blocked), kv, "shop.vn")
    assert result.error
    assert "blocked" in result.error
    assert not (await discover(session(blocked), kv, "shop.vn")).from_cache  # lỗi không cache


def test_sources_dedupe_careers_hosts() -> None:
    from jobcrawler.pipeline.discovery import DiscoveryResult

    result = DiscoveryResult(
        domain="a.vn", careers_pages=["https://a.vn/tuyen-dung", "https://a.vn/en/careers"]
    )
    sources = sources_for(result)
    assert len(sources) == 1
    assert isinstance(sources[0], CareersSource)


async def test_spa_soft_404_paths_are_skipped() -> None:
    """SPA trả nguyên khung trang chủ (200) cho mọi đường dẫn: không mở trình duyệt từng cái."""
    shell = '<html><body><div id="root"></div><script src="/app.js"></script></body></html>'
    fetcher = FakeFetcher()
    fetcher.add("https://shop.vn/", shell)
    for path in ("/careers", "/career", "/jobs"):
        fetcher.add(f"https://shop.vn{path}", shell)
    result = await discover(session(fetcher), MemoryKV(), "shop.vn")
    assert not result.found
    assert any("giống trang chủ" in step for step in result.steps)
    rendered = [url for kind, url, _ in fetcher.calls if kind == "render"]
    assert rendered == ["https://shop.vn/"]  # chỉ trang chủ


async def test_subdomain_is_probed_before_common_paths() -> None:
    fetcher = FakeFetcher()
    fetcher.add("https://acme.vn/", "<html><body>Trang chủ</body></html>")
    fetcher.add("https://tuyendung.acme.vn/", LISTING)
    fetcher.add("https://acme.vn/jobs", LISTING)
    result = await discover(session(fetcher), MemoryKV(), "acme.vn")
    assert result.method == "subdomain"
    assert result.careers_pages == ["https://tuyendung.acme.vn/"]
    opened = [url for _, url, _ in fetcher.calls]
    assert "https://acme.vn/jobs" not in opened


async def test_timeout_keeps_unconfirmed_careers_page() -> None:
    """Hết giờ trước khi xác nhận được trang nào: vẫn thử trang careers rõ nhất đã thấy."""

    class SlowSitemap(FakeFetcher):
        async def fetch(self, url: str, **kwargs: Any) -> FetchResult:
            if "sitemap" in url:
                await asyncio.sleep(30)
            return await super().fetch(url, **kwargs)

    fetcher = SlowSitemap()
    fetcher.add("https://acme.vn/", '<a href="/tuyen-dung">Tuyển dụng</a>')
    # Trang giới thiệu tuyển dụng, chưa thấy link tin nào.
    fetcher.add(
        "https://acme.vn/tuyen-dung",
        "<html><body><h1>Tuyển dụng</h1><p>"
        + "Gia nhập đội ngũ ACME. " * 20
        + "</p></body></html>",
    )
    result = await discover(session(fetcher), MemoryKV(), "acme.vn", timeout=0.5)
    assert result.error == "timeout"
    assert result.careers_pages == ["https://acme.vn/tuyen-dung"]
    assert result.method == "unconfirmed"


async def test_unresponsive_bare_domain_falls_back_to_www_quickly() -> None:
    """hoaphat.com.vn không phản hồi, www.hoaphat.com.vn thì có: dùng www ngay."""

    class SlowApex(FakeFetcher):
        async def fetch(self, url: str, **kwargs: Any) -> FetchResult:
            if url.startswith("https://acme.vn/"):
                await asyncio.sleep(30)  # không phản hồi
            return await super().fetch(url, **kwargs)

    fetcher = SlowApex()
    fetcher.add("https://www.acme.vn/", '<a href="/tuyen-dung">Tuyển dụng</a>')
    fetcher.add("https://www.acme.vn/tuyen-dung", LISTING)
    result = await asyncio.wait_for(
        discover(session(fetcher), MemoryKV(), "acme.vn", timeout=5), timeout=10
    )
    assert result.careers_pages == ["https://www.acme.vn/tuyen-dung"]


HTTP_HOME = '<html><body><a href="/tuyen-dung">Tuyển dụng</a></body></html>'
HTTP_LISTING = (
    "<html><body><h1>Tuyển dụng</h1><ul>"
    '<li><a href="/tuyen-dung/ke-toan-tong-hop-101">Kế toán tổng hợp</a></li>'
    '<li><a href="/tuyen-dung/nhan-vien-kinh-doanh-102">Nhân viên kinh doanh</a></li>'
    "</ul></body></html>"
)


async def test_http_only_site_is_found() -> None:
    """Website doanh nghiệp nhỏ chỉ có http: trang chủ thử cả http."""
    fetcher = FakeFetcher()
    fetcher.add("http://www.cokhi-abc.vn/", HTTP_HOME)
    fetcher.add("http://www.cokhi-abc.vn/tuyen-dung", HTTP_LISTING)
    result = await discover(session(fetcher), MemoryKV(), "cokhi-abc.vn")
    assert result.careers_pages == ["http://www.cokhi-abc.vn/tuyen-dung"]


async def test_input_origin_with_port_is_kept() -> None:
    fetcher = FakeFetcher()
    fetcher.add("http://jobs.local:8765/", HTTP_HOME)
    fetcher.add("http://jobs.local:8765/tuyen-dung", HTTP_LISTING)
    result = await discover(session(fetcher), MemoryKV(), "http://jobs.local:8765/")
    assert result.homepage == "http://jobs.local:8765/"
    assert result.careers_pages == ["http://jobs.local:8765/tuyen-dung"]
