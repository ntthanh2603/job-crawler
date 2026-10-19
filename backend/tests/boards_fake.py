"""Trang tuyển dụng giả cho test khung index (sitemap + JSON-LD), dùng chung test đơn vị và
tích hợp."""

from __future__ import annotations

import json
import re

from jobcrawler.fakes import FakeFetcher
from jobcrawler.sources.boards.base import SitemapBoard

HOME = "https://board.test/"


class FakeBoard(SitemapBoard):
    name = "fakeboard"
    home = HOME
    sitemaps = (f"{HOME}sitemap.xml",)
    job_sitemap = re.compile(r"sitemap-jobs-\d+\.xml$")
    job_url = re.compile(r"/viec-lam/[\w-]+-(?P<id>\d+)\.html$")
    profile_url = re.compile(r"/cong-ty/[\w-]+-(?P<id>\d+)$")


def sitemap_index(*children: str) -> str:
    items = "".join(f"<sitemap><loc>{HOME}{c}</loc></sitemap>" for c in children)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</sitemapindex>'
    )


def urlset(*jobs: tuple[str, str | None]) -> str:
    items = "".join(
        f"<url><loc>{HOME}{path}</loc>" + (f"<lastmod>{lm}</lastmod>" if lm else "") + "</url>"
        for path, lm in jobs
    )
    return f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</urlset>'


def job_page(
    title: str, company: str, company_path: str, *, valid_through: str = "2099-12-31"
) -> str:
    data = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": title,
        "description": "<p>" + "Mô tả công việc chi tiết. " * 20 + "</p>",
        "datePosted": "2026-10-01",
        "validThrough": valid_through,
        "hiringOrganization": {
            "@type": "Organization",
            "name": company,
            "sameAs": f"{HOME}{company_path}",
            "logo": f"{HOME}logo.png",
        },
        "jobLocation": {"@type": "Place", "address": {"addressLocality": "Hà Nội"}},
    }
    return (
        f'<html><head><script type="application/ld+json">{json.dumps(data, ensure_ascii=False)}'
        f'</script></head><body><h1>{title}</h1><a href="/cong-ty/x">Menu</a>'
        f'<a href="/{company_path}">{company}</a></body></html>'
    )


def fake_site(
    jobs: dict[str, tuple[str, str, str]], *, lastmod: dict[str, str] | None = None
) -> FakeFetcher:
    """jobs: path → (tiêu đề, tên công ty, đường dẫn profile)."""
    fetcher = FakeFetcher()
    fetcher.add(f"{HOME}sitemap.xml", sitemap_index("sitemap-jobs-1.xml", "sitemap-news.xml"))
    fetcher.add(
        f"{HOME}sitemap-jobs-1.xml",
        urlset(*((p, (lastmod or {}).get(p)) for p in jobs)),
        content_type="application/xml",
    )
    for path, (title, company, company_path) in jobs.items():
        fetcher.add(
            f"{HOME}{path}", job_page(title, company, company_path), headers={"etag": f'"{path}"'}
        )
    return fetcher
