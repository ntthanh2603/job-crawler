"""TopDev (topdev.vn, nay mang thương hiệu Saramin; có cả tin ngoài IT).

- Sitemap `sitemap.xml` → `sitemap-jobs.xml` → `sitemap/jobs_desc_vi_page_N.xml` (20 URL/trang,
  `lastmod` thật). Bản `_en_` trùng mã tin nên bỏ.
- Mã tin = số cuối URL `/viec-lam/<slug>-<id>`.
- JSON-LD: `hiringOrganization.sameAs` = profile `/nha-tuyen-dung/<slug>-<companyId>`;
  `identifier` là mã CÔNG TY (không phải mã tin) nên bỏ; `employmentType` luôn "OTHER".
- Trang profile: tên, website ("Website công ty"), địa chỉ; tin đang mở chỉ có trong dữ liệu
  React (không có link trong HTML) → crawl trực tiếp không liệt kê được, dùng tin đã index.
Mẫu: tests/fixtures/boards/topdev/.
"""

from __future__ import annotations

import re

from jobcrawler.net.urls import normalize_url
from jobcrawler.pages import clean_text, meta_content, parse
from jobcrawler.sources.boards.base import BoardJob, ProfileRef, SitemapBoard


class TopDev(SitemapBoard):
    name = "topdev"
    home = "https://topdev.vn/"
    sitemaps = ("https://topdev.vn/sitemap.xml",)
    job_sitemap = re.compile(r"(?:sitemap-jobs\.xml|jobs_desc_vi_page_\d+\.xml)$")
    job_url = re.compile(r"^https://topdev\.vn/viec-lam/[a-z0-9-]+-(?P<id>\d+)$")
    profile_url = re.compile(
        r"^https://topdev\.vn/(?:nha-tuyen-dung|companies)/[a-z0-9-]+-(?P<id>\d+)$"
    )
    profile_lists_jobs = False

    def parse_job(self, html: str, url: str) -> BoardJob | None:
        parsed = super().parse_job(html, url)
        if parsed is None:
            return None
        extra = {k: v for k, v in parsed.job.extra.items() if k != "identifier"}
        employment = parsed.job.employment_type
        job = parsed.job.model_copy(
            update={
                "extra": extra,
                "employment_type": None if employment == "OTHER" else employment,
            }
        )
        return BoardJob(job=job, profile=parsed.profile)

    def parse_profile(self, html: str, url: str) -> ProfileRef | None:
        url = self.canonical(url)
        external_id = self.profile_id(url)
        tree = parse(html)
        node = tree.css_first("span.text-xl.font-semibold")
        name = clean_text(node.text(deep=True, separator=" ")) if node is not None else ""
        if not name and (og := meta_content(tree, "og:title")):
            name = re.split(r"\s+tuyển dụng\b", og, maxsplit=1)[0].strip()
        if external_id is None or not name:
            return None
        website = None
        for a in tree.css("a[href]"):
            if "website" in a.text().lower() and (
                href := normalize_url(a.attributes.get("href") or "")
            ):
                website = href
                break
        info: dict[str, str] = {}
        for span in tree.css("span.flex.text-sm"):
            text = clean_text(span.text(deep=True, separator=" "))
            if re.search(r"\b(Phường|Xã|Quận|Huyện|Thành phố|Tỉnh|Đường)\b", text):
                info["address"] = text
                break
        return ProfileRef(external_id=external_id, url=url, name=name, website=website, info=info)
