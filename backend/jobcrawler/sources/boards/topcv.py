"""TopCV (topcv.vn): trang tuyển dụng lớn nhất (~45 nghìn tin).

- Sitemap `sitemap.xml` → `sitemap/jobs.xml` → `jobs_0..N.xml` (200 URL/file, `lastmod` thật).
- Lượt đuôi: `sitemap/jobs.xml` (index ~22 KB, không có `lastmod`) → 3 file số lớn nhất (tin
  mới nhất nằm ở cuối danh sách; từng file được cache riêng nên tin mới có khi rơi vào file áp
  chót), xếp theo `lastmod`: 4 request (~150 KB). Sitemap sinh lại khoảng mỗi giờ (10/2026: tin
  mới nhất trong sitemap cũ ~1 giờ) — trang "việc làm mới nhất" xếp theo giờ cập nhật (~50 tin
  được đẩy lên mỗi phút, 2 MB/trang) nên không dùng được cho tin mới.
- URL tin: `/viec-lam/<slug>/<id>.html`, tin của thương hiệu
  `/brand/<b>/tuyen-dung/<slug>-j<id>.html`.
- JSON-LD: `hiringOrganization.sameAs` = profile trên TopCV (`/cong-ty/<slug>/<id>.html`, thương
  hiệu: `/brand/<slug>?id=<id>`); `identifier` là mã công ty.
- Trang profile: Organization JSON-LD (website ở `sameAs`, địa chỉ); tin đang mở ở trang riêng
  `/cong-ty/<slug>-cid<id>/tuyen-dung.html` (thương hiệu: `/brand/<slug>/tuyen-dung?id=<id>`),
  chia trang bằng JavaScript ("1/2 trang", `data-page-url`): trang sau đọc bằng `?page=N`.
- Trang chi tiết ghi ngoài JSON-LD: khối "Thông tin chung" (cấp bậc, học vấn, số lượng tuyển,
  hình thức làm việc; tin trả phí dùng bố cục `general-information-data`) và "Danh mục Nghề liên
  quan" — đường danh mục gốc → nhánh → lá, link `/tim-viec-lam-<slug>-cr<gốc>cb<nhánh>cl<lá>`.
  JSON-LD `industry` là bộ nhãn ngành cũ do nhà tuyển dụng tự chọn (nhiều nhãn thừa).
Mẫu: tests/fixtures/boards/topcv/.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from selectolax.lexbor import LexborHTMLParser

from jobcrawler.common.pages import node_text, parse
from jobcrawler.common.schemas import RawJob
from jobcrawler.common.text import fold
from jobcrawler.sources.boards.base import ProfilePage, SitemapBoard

_BRAND = re.compile(r"^https://www\.topcv\.vn/brand/(?P<slug>[^/?]+)$")
# "<span class="hight-light">1</span>/2 trang" trong #job-listing-paginate-text.
_PAGES = re.compile(r"(\d+)\s*/\s*(\d+)\s*trang")
_COMPANY = re.compile(r"^https://www\.topcv\.vn/cong-ty/(?P<slug>[^/]+)/(?P<id>\d+)\.html$")
# Khối "Thông tin chung": (ô, nhãn, giá trị) của bố cục thường và bố cục tin trả phí.
_INFO = (
    (
        ".box-job-information-general-info-list__item--content",
        ".box-job-information-general-info-list__item--content-title",
        ".box-job-information-general-info-list__item--content-desc",
    ),
    (
        ".general-information-data",
        ".general-information-data__label",
        ".general-information-data__value",
    ),
)
# Link danh mục nghề: gốc `-cr<id>`, nhánh `cb<id>`, lá `cl<id>` (link tỉnh / từ khoá không có).
_CATEGORY_LINK = re.compile(r"/tim-viec-lam-[^/?#]+-cr\d+(?:cb\d+)?(?:cl\d+)?/?$")
_COUNT = re.compile(r"\d+")


def general_info(tree: LexborHTMLParser) -> dict[str, str]:
    """Khối "Thông tin chung": nhãn (đã fold, vd. `hoc van`) → giá trị."""
    info: dict[str, str] = {}
    for box, label_selector, value_selector in _INFO:
        for node in tree.css(box):
            key = fold(node_text(node.css_first(label_selector)))
            value = node_text(node.css_first(value_selector))
            if key and value:
                info.setdefault(key, value)
    return info


def category_path(tree: LexborHTMLParser) -> list[str]:
    """Đường danh mục nghề của tin (gốc → nhánh → lá) trong "Danh mục Nghề liên quan"."""
    for box in tree.css(".box-category"):
        path: dict[str, None] = {}
        for link in box.css("a.box-category-tag"):
            href = (link.attributes.get("href") or "").split("?")[0]
            if _CATEGORY_LINK.search(href) and (text := node_text(link)):
                path.setdefault(text, None)
        if path:
            return list(path)
    return []


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
    tail_sitemaps = ("https://www.topcv.vn/sitemap/jobs.xml",)
    tail_files = 3

    def canonical(self, url: str) -> str:
        base = super().canonical(url)
        # Profile thương hiệu có mã trong query: /brand/ipa?id=219468.
        if _BRAND.match(base):
            ids = parse_qs(urlsplit(url).query).get("id")
            if ids and ids[0].isdigit():
                return f"{base}?id={ids[0]}"
        return base

    def enrich_job(self, job: RawJob, tree: LexborHTMLParser) -> RawJob:
        info = general_info(tree)
        extra = dict(job.extra)
        updates: dict[str, object] = {}
        if education := info.get("hoc van"):
            extra["education"] = education
        if "openings" not in extra and (m := _COUNT.search(info.get("so luong tuyen", ""))):
            extra["openings"] = int(m.group())
        if path := category_path(tree):
            extra["category_path"] = path
        # JSON-LD của TopCV không có `jobLocationType`: "Làm việc tại văn phòng / Onsite".
        if job.work_mode is None and (mode := info.get("hinh thuc lam viec")):
            updates["work_mode"] = mode
        if job.employment_type is None and (kind := info.get("loai hinh lam viec")):
            updates["employment_type"] = kind
        if (
            not job.level
            and not extra.get("occupational_category")
            and (lvl := info.get("cap bac"))
        ):
            updates["level"] = lvl
        if extra != job.extra:
            updates["extra"] = extra
        return job.model_copy(update=updates) if updates else job

    def profile_id(self, url: str) -> str | None:
        m = self.profile_url.search(self.canonical(url))
        return (m.group("id") or m.group("bid")) if m else None

    def profile_page(self, html: str, url: str) -> ProfilePage:
        urls = self.profile_job_urls(html, url)
        tree = parse(html)
        box = tree.css_first("#job-listing-paginate-text")
        m = _PAGES.search(box.text(deep=True, separator=" ")) if box is not None else None
        if m is None:
            # Không có phân trang: một trang. Không chắc là đủ (trang có thể đổi bố cục).
            return ProfilePage(urls=urls)
        current, total = int(m[1]), int(m[2])
        if current >= total:
            return ProfilePage(urls=urls, last=True)
        node = tree.css_first("[data-page-url]")
        base = (node.attributes.get("data-page-url") if node is not None else None) or url
        base = base.split("?")[0]
        brand_id = parse_qs(urlsplit(url).query).get("id")
        query = f"id={brand_id[0]}&" if brand_id and "/brand/" in base else ""
        return ProfilePage(urls=urls, next_url=f"{base}?{query}page={current + 1}")

    def profile_jobs_url(self, profile_url: str) -> str:
        url = self.canonical(profile_url)
        if m := _COMPANY.match(url):
            return f"{self.home}cong-ty/{m['slug']}-cid{m['id']}/tuyen-dung.html"
        if (m := _BRAND.match(url.split("?")[0])) and (pid := self.profile_id(url)):
            return f"{self.home}brand/{m['slug']}/tuyen-dung?id={pid}"
        return url
