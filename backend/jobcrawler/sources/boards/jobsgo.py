"""JobsGO (jobsgo.vn): đa ngành, nhiều tin lao động phổ thông.

- Sitemap index thật là `sitemap.xml` (robots chỉ ghi sitemap blog) → `sitemap-job.xml`: một file
  ~5 MB, ~28 nghìn URL, `lastmod` thật (sitemap phủ ~87% tin trên trang danh sách).
- Lượt đuôi: thẳng `sitemap-job.xml` (đã xếp `lastmod` mới trước, sinh lại mỗi giờ, có
  Last-Modified) kèm If-Modified-Since theo lượt trước: 1 request, phần lớn là 304; chỉ tải lại
  (~5 MB, nén gzip khi truyền) sau mỗi lần sinh lại. Server bỏ qua Range (nén khi gửi) nên không
  đọc được riêng phần đầu. Trang danh sách `viec-lam.html?sort=created` có khối tin ghim lẫn vào
  và nhãn "cập nhật N phút trước" chung cho cả trang: không chắc hơn sitemap.
- Mã tin = số 11 chữ số cuối URL `/viec-lam/<slug>-<id>.html`.
- Hai mẫu trang chi tiết: thường (`hiringOrganization.url` = profile) và "việc nhanh"
  (`hiringOrganization.sameAs` = profile). Trang còn có Organization JSON-LD của chính JobsGO:
  không lấy làm nhà tuyển dụng (chỉ đọc `hiringOrganization` của JobPosting).
- Profile `/tuyen-dung/<slug>-<id>.html` hoặc `/brand/<slug>-<id>.html`: tên, link tin đang mở,
  MST thường nằm trong phần mô tả ("Mã số thuế 0107593793"); không có website. Một doanh nghiệp
  có thể có nhiều profile (theo canonical).
Mẫu: tests/fixtures/boards/jobsgo/.
"""

from __future__ import annotations

import re

from jobcrawler.pages import clean_text, parse
from jobcrawler.pipeline.company_facts import tax_codes_in
from jobcrawler.sources.boards.base import ProfileRef, SitemapBoard


class JobsGO(SitemapBoard):
    name = "jobsgo"
    home = "https://jobsgo.vn/"
    sitemaps = ("https://jobsgo.vn/sitemap.xml",)
    job_sitemap = re.compile(r"/sitemap-job\.xml$")
    job_url = re.compile(r"^https://jobsgo\.vn/viec-lam/[a-z0-9-]+-(?P<id>\d{8,})\.html$")
    profile_url = re.compile(
        r"^https://jobsgo\.vn/(?:tuyen-dung|brand)/[a-z0-9-]*-(?P<id>\d{6,})\.html$"
    )
    tail_sitemaps = ("https://jobsgo.vn/sitemap-job.xml",)
    tail_conditional = True

    def parse_profile(self, html: str, url: str) -> ProfileRef | None:
        tree = parse(html)
        canonical = tree.css_first("link[rel=canonical]")
        href = canonical.attributes.get("href") if canonical is not None else None
        if href and self.profile_id(href):
            url = href  # trùng lặp: theo profile chính
        url = self.canonical(url)
        external_id = self.profile_id(url)
        h1 = tree.css_first("h1")
        name = clean_text(h1.text(deep=True, separator=" ")) if h1 is not None else ""
        if external_id is None or not name:
            return None
        info: dict[str, str] = {}
        description = tree.css_first("#company-description")
        text = description.text(deep=True, separator="\n") if description is not None else ""
        tax_codes = tax_codes_in(text)
        if m := re.search(r"Địa chỉ\s*[:\t ]\s*([^\n]+)", text):
            info["address"] = clean_text(m.group(1))
        return ProfileRef(
            external_id=external_id,
            url=url,
            name=name,
            tax_code=tax_codes[0] if tax_codes else None,
            info=info,
        )
