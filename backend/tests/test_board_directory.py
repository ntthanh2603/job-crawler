from __future__ import annotations

import gzip
import re

from jobcrawler.pipeline.board_directory import matching, read_directory, slug_name
from jobcrawler.services.fakes import FakeChatClient, FakeFetcher
from jobcrawler.services.llm import LLMClient
from jobcrawler.sources.base import SourceLimits, SourceSession
from jobcrawler.sources.boards import registry
from jobcrawler.sources.boards.base import SitemapBoard

from .boards_fake import HOME, sitemap_index, urlset


class DirectoryBoard(SitemapBoard):
    name = "directoryboard"
    home = HOME
    sitemaps = (f"{HOME}sitemap.xml",)
    job_sitemap = re.compile(r"sitemap-jobs-\d+\.xml$")
    job_url = re.compile(r"/viec-lam/[\w-]+-(?P<id>\d+)\.html$")
    profile_url = re.compile(r"/cong-ty/(?P<slug>[\w-]+)-(?P<id>\d+)$")
    placeholder_profiles = frozenset({"1"})
    company_sitemaps = (f"{HOME}companies.xml",)
    company_sitemap = re.compile(r"companies-\d+\.xml(?:\.gz)?$")


def session(fetcher: FakeFetcher) -> SourceSession:
    llm = LLMClient(FakeChatClient([]), model=None)
    return SourceSession("board:directoryboard", fetcher, llm, SourceLimits(), [])


async def test_read_directory_follows_index_and_names_profiles_from_slug() -> None:
    fetcher = FakeFetcher()
    fetcher.add(
        f"{HOME}companies.xml",
        sitemap_index("companies-1.xml", "companies-2.xml.gz", "companies-3.xml", "news.xml"),
    )
    fetcher.add(
        f"{HOME}companies-1.xml",
        urlset(
            ("cong-ty/cong-ty-co-phan-mat-bao-7", None),
            ("cong-ty/an-danh-1", None),  # profile giữ chỗ
            ("viec-lam/ke-toan-11.html", None),  # không phải profile
        ),
    )
    fetcher.add(
        f"{HOME}companies-2.xml.gz",
        gzip.compress(urlset(("cong-ty/fpt-software-8", None)).encode()),
    )
    fetcher.add(f"{HOME}companies-3.xml", "<html>Không tìm thấy</html>")

    found = await read_directory(DirectoryBoard(), session(fetcher))

    assert {(e.external_id, e.name) for e in found.entries} == {
        ("7", "cong ty co phan mat bao"),
        ("8", "fpt software"),
    }
    assert found.sitemaps == 4
    assert found.errors == [f"{HOME}companies-3.xml"]
    assert f"{HOME}news.xml" not in [url for _, url, _ in fetcher.calls]


async def test_read_directory_stops_at_sitemap_cap() -> None:
    fetcher = FakeFetcher()
    fetcher.add(f"{HOME}companies.xml", sitemap_index("companies-1.xml", "companies-2.xml"))
    fetcher.add(f"{HOME}companies-1.xml", urlset(("cong-ty/acme-7", None)))

    found = await read_directory(DirectoryBoard(), session(fetcher), max_sitemaps=2)

    assert [e.external_id for e in found.entries] == ["7"]
    assert found.errors == ["vượt 2 sitemap"]


def test_matching_compares_company_match_keys() -> None:
    from jobcrawler.common.text import company_match_key
    from jobcrawler.pipeline.board_directory import DirectoryEntry

    entries = [
        DirectoryEntry("7", f"{HOME}cong-ty/x-7", slug_name("cong-ty-co-phan-mat-bao")),
        DirectoryEntry("8", f"{HOME}cong-ty/x-8", slug_name("ngan-hang-tmcp-quan-doi")),
        DirectoryEntry("9", f"{HOME}cong-ty/x-9", slug_name("cong-ty-tnhh-mat-bao-bpo")),
    ]
    keys = {
        company_match_key("Công ty Cổ phần Mắt Bão"),
        company_match_key("Ngân hàng Thương mại Cổ phần Quân Đội (MB)"),
    }
    assert [e.external_id for e in matching(entries, keys)] == ["7", "8"]


def test_board_adapters_read_name_from_profile_url() -> None:
    boards = registry()
    cases = {
        "topcv": ("https://www.topcv.vn/cong-ty/cong-ty-co-phan-mat-bao/147049.html", "147049"),
        "jobsgo": (
            "https://jobsgo.vn/tuyen-dung/cong-ty-co-phan-duoc-pham-thai-minh-282388884.html",
            "282388884",
        ),
        "itviec": ("https://itviec.com/nha-tuyen-dung/zalopay", "zalopay"),
        "vieclam24h": (
            "https://vieclam24h.vn/danh-sach-tin-tuyen-dung-cong-ty-co-phan-canifa-ntd20356093p122.html",
            "20356093",
        ),
        "careerviet": (
            "https://careerviet.vn/vi/nha-tuyen-dung/home-credit-vietnam.35A51A1A.html",
            "35A51A1A",
        ),
    }
    slugs = {}
    for name, (url, external_id) in cases.items():
        adapter = boards[name]
        assert adapter.company_sitemaps, name
        assert adapter.profile_id(url) == external_id, name
        slugs[name] = adapter.profile_slug(url)
    assert slugs == {
        "topcv": "cong-ty-co-phan-mat-bao",
        "jobsgo": "cong-ty-co-phan-duoc-pham-thai-minh",
        "itviec": "zalopay",
        "vieclam24h": "cong-ty-co-phan-canifa",
        "careerviet": "home-credit-vietnam",
    }
    # Profile thương hiệu TopCV không có tên trong URL.
    assert boards["topcv"].profile_slug("https://www.topcv.vn/brand/ipa?id=219468") is None
