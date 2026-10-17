"""CareerLink (careerlink.vn): không có sitemap XML.

- Danh sách `/vieclam/list?order=posted_date&sort=desc&page=N` (50 tin/trang, mới nhất trước,
  ~700 trang): đi hết mỗi lượt để biết tin nào đã gỡ.
- URL tin `/tim-viec-lam/<slug>/<id>`; `identifier` của JobPosting là mã tin.
- `hiringOrganization.sameAs` = profile `/viec-lam-cua/<slug>/<id>`. Profile 3470 ("CareerLink's
  client") là chỗ giữ cho tin ẩn danh/headhunter: không phải nhà tuyển dụng thật.
- Trang profile: website `#company-media a[itemprop=url]`, link tin đang mở; không có MST.
Mẫu: tests/fixtures/boards/careerlink/.
"""

from __future__ import annotations

import re

from jobcrawler.net.urls import normalize_url
from jobcrawler.pages import parse
from jobcrawler.sources.boards.base import ListingBoard, ProfileRef


class CareerLink(ListingBoard):
    name = "careerlink"
    home = "https://www.careerlink.vn/"
    listing_url = "https://www.careerlink.vn/vieclam/list?order=posted_date&sort=desc&page={page}"
    max_listing_pages = 900
    # Trang hiện CAPTCHA sau vài chục request liên tiếp ở 1 req/s: đi chậm hơn hẳn.
    rate = 0.2
    note = "chậm: 1 request / 5 giây (CAPTCHA)"
    job_url = re.compile(r"^https://www\.careerlink\.vn/tim-viec-lam/[^/]+/(?P<id>\d+)$")
    profile_url = re.compile(r"^https://www\.careerlink\.vn/viec-lam-cua/[^/]+/(?P<id>\d+)$")
    profile_name_selector = ".company-name"
    profile_selector = 'a.text-reset[href^="/viec-lam-cua/"]'
    identifier_is_job = True
    placeholder_profiles = frozenset({"3470"})

    def parse_profile(self, html: str, url: str) -> ProfileRef | None:
        profile = super().parse_profile(html, url)
        if profile is None or profile.external_id in self.placeholder_profiles:
            return None
        node = parse(html).css_first("#company-media a[itemprop=url]")
        href = normalize_url(node.attributes.get("href") or "") if node is not None else None
        if href:
            profile = profile.model_copy(update={"website": href})
        return profile
