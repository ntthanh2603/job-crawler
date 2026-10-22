"""ITviec (itviec.com): việc làm IT.

- Sitemap index `dunggiatminh.xml` → `twinnings_jobs_desc_vn.xml` (một file, mọi tin đang mở).
  `lastmod` = giờ sinh sitemap cho mọi URL nên bỏ qua; tin đổi nội dung được bắt bằng làm mới
  định kỳ + ETag.
- Lượt đuôi: thẳng `twinnings_jobs_desc_vn.xml` (sinh theo request nên luôn mới; ~650 tin): 1
  request; lượt index chỉ tải tin chưa có.
- Mã tin = cả slug (`...-viettel-group-3452`, có thể có `_`): 4 chữ số cuối không duy nhất.
- JSON-LD JobPosting có lương (dù giao diện ẩn), nhưng tiêu đề đã bỏ dấu → lấy `<h1>`.
- Profile: `.job-show-employer-info h3 a` → `/nha-tuyen-dung/<slug>`; trang profile có website
  (`data-redirect-url-url-value`), địa chỉ, link tin đang mở; không có MST.
Mẫu: tests/fixtures/boards/itviec/.
"""

from __future__ import annotations

import re

from jobcrawler.common.pages import clean_text, parse
from jobcrawler.common.urls import host_of, normalize_url, same_site
from jobcrawler.sources.boards.base import ProfileRef, SitemapBoard, is_social_host


class ITviec(SitemapBoard):
    name = "itviec"
    home = "https://itviec.com/"
    sitemaps = ("https://itviec.com/dunggiatminh.xml",)
    job_sitemap = re.compile(r"twinnings_jobs_desc_vn\.xml$")
    job_url = re.compile(r"^https://itviec\.com/viec-lam-it/(?P<id>[a-z0-9_-]+-\d+)$")
    profile_url = re.compile(r"^https://itviec\.com/nha-tuyen-dung/(?P<id>(?P<slug>[a-z0-9-]+))$")
    trust_lastmod = False
    # Cloudflare chặn sau ~50 trang ở 1 req/s (một IP, không proxy).
    rate = 1 / 3
    note = "chậm: 1 request / 3 giây (Cloudflare)"
    title_selector = "h1"
    profile_selector = ".job-show-employer-info h3 a"
    tail_sitemaps = ("https://itviec.com/twinnings_jobs_desc_vn.xml",)
    # Danh bạ nhà tuyển dụng: một file (~12 nghìn profile, 10/2026); mã profile là slug tên.
    company_sitemaps = ("https://itviec.com/twinnings_companies_vn.xml",)

    def parse_profile(self, html: str, url: str) -> ProfileRef | None:
        url = self.canonical(url)
        external_id = self.profile_id(url)
        tree = parse(html)
        h1 = tree.css_first("h1")
        name = clean_text(h1.text(deep=True, separator=" ")) if h1 is not None else ""
        if external_id is None or not name:
            return None
        website = None
        for node in tree.css("[data-redirect-url-url-value]"):
            value = normalize_url(node.attributes.get("data-redirect-url-url-value") or "")
            if value and not same_site(value, self.home) and not is_social_host(host_of(value)):
                website = value
                break
        info: dict[str, str] = {}
        place = tree.css_first("[data-location]")
        if place is not None and (address := clean_text(place.attributes.get("data-location"))):
            info["address"] = address
        return ProfileRef(external_id=external_id, url=url, name=name, website=website, info=info)
