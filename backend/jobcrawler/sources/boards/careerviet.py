"""CareerViet (careerviet.vn).

- Sitemap `sitemap/sitemap.xml` → `job_vi_0..2.xml` (~6.500 URL/file; bản `job_en_*` trùng mã).
  `lastmod` ghi đuôi `Z` nhưng thực là giờ Việt Nam: chỉ dùng để so trước/sau nên không ảnh hưởng.
- Lượt đuôi: thẳng `sitemap/job_current_date.xml` — tin đăng trong ngày (giờ Việt Nam), sinh mới
  theo request (10/2026 có cả tin chưa lên trang danh sách), ~100 KB, mã tăng dần theo thứ tự
  file, `lastmod` chỉ có ngày: 1 request. Tin đăng sát nửa đêm do lượt index đủ ban đêm bắt.
- URL tin `/vi/tim-viec-lam/<slug>.<MÃ hex 8 ký tự>.html`; `identifier` của JobPosting là mã tin.
- JSON-LD: `hiringOrganization.url` = profile trên CareerViet, `sameAs` = website doanh nghiệp;
  `employmentType` bị bọc thêm dấu nháy (`"\\"FULL_TIME\\""`).
- Trang profile: Organization JSON-LD (website, địa chỉ); MST chỉ có trong dữ liệu trang
  (`emp_tax_id`), 10 link tin đang mở.
Mẫu: tests/fixtures/boards/careerviet/.
"""

from __future__ import annotations

import re

from jobcrawler.sources.boards.base import BoardJob, ProfileRef, SitemapBoard

_TAX = re.compile(r'emp_tax_id\\?"\s*:\s*\\?"(\d{10}(?:-\d{3})?)\\?"')


class CareerViet(SitemapBoard):
    name = "careerviet"
    home = "https://careerviet.vn/"
    sitemaps = ("https://careerviet.vn/sitemap/sitemap.xml",)
    job_sitemap = re.compile(r"/job_vi_\d+\.xml$")
    job_url = re.compile(
        r"^https://careerviet\.vn/vi/tim-viec-lam/[^/]+\.(?P<id>[0-9A-Fa-f]{8})\.html$"
    )
    profile_url = re.compile(
        r"^https://careerviet\.vn/vi/nha-tuyen-dung/[^/]+\.(?P<id>[0-9A-Fa-f]{8})\.html$"
    )
    identifier_is_job = True
    tail_sitemaps = ("https://careerviet.vn/sitemap/job_current_date.xml",)

    def parse_job(self, html: str, url: str) -> BoardJob | None:
        parsed = super().parse_job(html, url)
        if parsed is None or not parsed.job.employment_type:
            return parsed
        cleaned = parsed.job.employment_type.replace('"', "").replace("\\", "").strip() or None
        return BoardJob(
            job=parsed.job.model_copy(update={"employment_type": cleaned}), profile=parsed.profile
        )

    def parse_profile(self, html: str, url: str) -> ProfileRef | None:
        profile = super().parse_profile(html, url)
        if profile is not None and (m := _TAX.search(html)):
            profile = profile.model_copy(update={"tax_code": m.group(1)})
        return profile
