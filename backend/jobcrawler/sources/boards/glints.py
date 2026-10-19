"""Glints Việt Nam (glints.com/vn).

- Sitemap index (~4.900 sitemap con, không có `lastmod`) → `sitemap_job_vn_1..N.xml` (mỗi tin
  có bản vi `/vn/...` và en `/vn/en/...`: chỉ lấy bản vi). Không có `lastmod`: so tập URL giữa
  các lượt, tin cũ làm mới định kỳ + ETag.
- Lượt đuôi: index (~370 KB) → 2 file `sitemap_job_vn_N` số lớn nhất (~50 tin/file, file cuối
  chưa đầy: tin mới được thêm vào cuối): 3 request. Trang tìm việc xếp mới nhất
  (`/opportunities/jobs/explore?...`) bị robots cấm.
- URL tin `/vn/opportunities/jobs/<slug>/<UUID>`; canonical của trang bị hỏng (`/jobs/id`).
- JSON-LD: `hiringOrganization.sameAs` = website doanh nghiệp; `identifier` = UUID công ty, khớp
  link profile `/vn/companies/<slug>/<UUID>`.
- Trang profile: Organization JSON-LD (website ở `url`, địa chỉ); không có MST.
Mẫu: tests/fixtures/boards/glints/.
"""

from __future__ import annotations

import re

from jobcrawler.sources.boards.base import SitemapBoard

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


class Glints(SitemapBoard):
    name = "glints"
    home = "https://glints.com/vn/"
    sitemaps = ("https://glints.com/sitemap_index.xml",)
    job_sitemap = re.compile(r"/sitemap_job_vn_\d+\.xml$")
    job_url = re.compile(rf"^https://glints\.com/vn/opportunities/jobs/[^/]+/(?P<id>{_UUID})$")
    profile_url = re.compile(rf"^https://glints\.com/vn/companies/[^/]+/(?P<id>{_UUID})$")
    trust_lastmod = False
    tail_files = 2
