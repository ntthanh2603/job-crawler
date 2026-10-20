"""TopDev (topdev.vn, nay mang thương hiệu Saramin; có cả tin ngoài IT).

- Sitemap `sitemap.xml` → `sitemap-jobs.xml` → `sitemap/jobs_desc_vi_page_N.xml` (20 URL/trang,
  `lastmod` thật). Bản `_en_` trùng mã tin nên bỏ.
- Lượt đuôi: thẳng `jobs_desc_vi_page_1..3.xml` (xếp `lastmod` = giờ làm mới tin, mới trước;
  ~7 KB/trang): 3 request, 60 tin mới nhất. Trang làm mới tin theo đợt (02:00 hằng ngày), tin mới
  đăng trong ngày nằm ở đầu nếu giờ làm mới của nó sau đợt đó.
- Mã tin = số cuối URL `/viec-lam/<slug>-<id>`.
- JSON-LD: `hiringOrganization.sameAs` = profile `/nha-tuyen-dung/<slug>-<companyId>`;
  `identifier` là mã CÔNG TY (không phải mã tin) nên bỏ; `employmentType` luôn "OTHER".
- `description` theo khuôn "<giới thiệu><br>Your role & responsibilities<br>…<br>Your skills &
  qualifications<br>…<br>Benefits for you<br>…", nhiều tin để trống hai mục đầu: nội dung chỉ có
  trong HTML (`<span>` tiêu đề mục + `div.prose-ul`) → lấy mô tả/yêu cầu từ đó, quyền lợi vẫn từ
  `jobBenefits`.
- Trang profile: tên, website ("Website công ty"), địa chỉ; tin đang mở chỉ có trong dữ liệu
  React (không có link trong HTML) → crawl trực tiếp không liệt kê được, dùng tin đã index.
Mẫu: tests/fixtures/boards/topdev/.
"""

from __future__ import annotations

import html as htmllib
import re
from typing import Any

from selectolax.lexbor import LexborHTMLParser

from jobcrawler.common.pages import clean_text, meta_content, node_text, parse
from jobcrawler.common.schemas import RawJob
from jobcrawler.common.text import fold
from jobcrawler.common.urls import normalize_url
from jobcrawler.sources.boards.base import BoardJob, ProfileRef, SitemapBoard

_LD_HEADING = re.compile(
    r"<br\s*/?>\s*(Your role &(?:amp;)? responsibilities|Your skills &(?:amp;)? qualifications|"
    r"Benefits for you)\s*<br\s*/?>",
    re.IGNORECASE,
)
# Tiêu đề mục (JSON-LD tiếng Anh, HTML tiếng Việt) → mục.
_SECTION_WORDS = (
    ("role", ("vai tro", "trach nhiem", "responsibilit")),
    ("skills", ("ky nang", "trinh do", "skill", "qualification")),
    ("benefits", ("quyen loi", "benefit")),
)

Section = tuple[str, str]  # (tiêu đề, HTML nội dung)


def _section_key(heading: str) -> str | None:
    folded = fold(heading)
    return next((k for k, words in _SECTION_WORDS if any(w in folded for w in words)), None)


def _is_empty(fragment: str | None) -> bool:
    return not fragment or not node_text(parse(fragment).body)


def _ld_sections(description: str) -> dict[str, Section] | None:
    """Các mục của `description` JSON-LD (khoá "intro" là phần trước mục đầu); None: không theo
    khuôn của TopDev."""
    parts = _LD_HEADING.split(description)
    if len(parts) < 3:
        return None
    sections = {"intro": ("", parts[0])}
    for heading, body in zip(parts[1::2], parts[2::2], strict=True):
        if (key := _section_key(heading)) is not None:
            sections.setdefault(key, (htmllib.unescape(heading), body))
    return sections


def _page_sections(tree: LexborHTMLParser) -> dict[str, Section]:
    """Các mục JD hiển thị trên trang: `<span>` tiêu đề (số thứ tự trong `<div>` con) ngay trước
    `<div>` bọc `div.prose-ul`."""
    found: dict[str, Section] = {}
    for body in tree.css("div.prose-ul"):
        heading = body.parent.prev if body.parent is not None else None
        while heading is not None and heading.tag != "span":
            heading = heading.prev
        if heading is None or not node_text(body):
            continue
        label = clean_text(heading.text(deep=False))
        if (key := _section_key(label)) is not None:
            found.setdefault(key, (label, body.inner_html or ""))
    return found


def _page_jd(html: str, job: RawJob) -> dict[str, Any]:
    """Mô tả/yêu cầu từ HTML khi các mục đó trong JSON-LD chỉ có tiêu đề ({} nếu không cần)."""
    ld = _ld_sections(job.description or "")
    if ld is None:
        return {}
    intro = ld.pop("intro")[1]
    sections = {k: v for k, v in ld.items() if not _is_empty(v[1])}
    missing = [k for k in ("role", "skills") if k not in sections]
    if not missing:
        return {}
    page = _page_sections(parse(html))
    if not any(k in page for k in missing):
        return {}  # trang cũng không có: giữ nguyên, để bước xác minh loại tin
    for key, section in page.items():
        sections.setdefault(key, section)
    description = "" if _is_empty(intro) else intro
    if "role" in sections:
        heading, body = sections["role"]
        description += f"<h3>{htmllib.escape(heading, quote=False)}</h3>{body}"
    return {
        "description": description or None,
        "requirements": sections["skills"][1] if "skills" in sections else job.requirements,
        "benefits": job.benefits or (sections["benefits"][1] if "benefits" in sections else None),
    }


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
    tail_sitemaps = tuple(
        f"https://topdev.vn/sitemap/jobs_desc_vi_page_{page}.xml" for page in (1, 2, 3)
    )

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
                **_page_jd(html, parsed.job),
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
