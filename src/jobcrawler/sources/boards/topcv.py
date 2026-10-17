"""TopCV (topcv.vn): trang tuyển dụng lớn nhất (~45 nghìn tin).

- Sitemap `sitemap.xml` → `sitemap/jobs.xml` → `jobs_0..N.xml` (200 URL/file, `lastmod` thật).
- URL tin: `/viec-lam/<slug>/<id>.html`, tin của thương hiệu
  `/brand/<b>/tuyen-dung/<slug>-j<id>.html`.
- JSON-LD: `hiringOrganization.sameAs` = profile trên TopCV (`/cong-ty/<slug>/<id>.html`, thương
  hiệu: `/brand/<slug>?id=<id>`); `identifier` là mã công ty.
- Trang profile: Organization JSON-LD (website ở `sameAs`, địa chỉ); tin đang mở ở trang riêng
  `/cong-ty/<slug>-cid<id>/tuyen-dung.html` (thương hiệu: `/brand/<slug>/tuyen-dung?id=<id>`).
Mẫu: tests/fixtures/boards/topcv/.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from jobcrawler.sources.boards.base import SitemapBoard

_BRAND = re.compile(r"^https://www\.topcv\.vn/brand/(?P<slug>[^/?]+)$")
_COMPANY = re.compile(r"^https://www\.topcv\.vn/cong-ty/(?P<slug>[^/]+)/(?P<id>\d+)\.html$")


class TopCV(SitemapBoard):
    name = "topcv"
    home = "https://www.topcv.vn/"
    sitemaps = ("https://www.topcv.vn/sitemap.xml",)
    job_sitemap = re.compile(r"/sitemap/jobs(?:_\d+)?\.xml$")
    job_url = re.compile(
        r"^https://www\.topcv\.vn/(?:viec-lam/[^/]+/|brand/[^/]+/tuyen-dung/[^/]+-j)"
        r"(?P<id>\d+)\.html$"
    )
    profile_url = re.compile(
        r"^https://www\.topcv\.vn/(?:cong-ty/[^/]+/(?P<id>\d+)\.html|brand/[^/?]+\?id=(?P<bid>\d+))$"
    )
    profile_selector = ".box-company-info__detail a.name"

    def canonical(self, url: str) -> str:
        base = super().canonical(url)
        # Profile thương hiệu có mã trong query: /brand/ipa?id=219468.
        if _BRAND.match(base):
            ids = parse_qs(urlsplit(url).query).get("id")
            if ids and ids[0].isdigit():
                return f"{base}?id={ids[0]}"
        return base

    def profile_id(self, url: str) -> str | None:
        m = self.profile_url.search(self.canonical(url))
        return (m.group("id") or m.group("bid")) if m else None

    def profile_jobs_url(self, profile_url: str) -> str:
        url = self.canonical(profile_url)
        if m := _COMPANY.match(url):
            return f"{self.home}cong-ty/{m['slug']}-cid{m['id']}/tuyen-dung.html"
        if (m := _BRAND.match(url.split("?")[0])) and (pid := self.profile_id(url)):
            return f"{self.home}brand/{m['slug']}/tuyen-dung?id={pid}"
        return url
