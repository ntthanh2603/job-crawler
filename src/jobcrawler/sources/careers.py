"""Trang careers chung trên website doanh nghiệp: JSON-LD → parser → LLM fallback.

Một `CareersSource` bắt đầu từ một trang danh sách (do `pipeline/discovery.py` tìm ra):

1. Đọc trang danh sách, lấy JobPosting JSON-LD có sẵn, link tới trang chi tiết, link phân
   trang / "xem tất cả" / mục lục việc làm (có giới hạn). HTML là khung SPA, hoặc HTTP không
   thấy tin nào, thì mở bằng trình duyệt và chờ mạng yên (danh sách nạp bằng XHR).
2. Không thấy gì bằng rule → LLM đọc trang (từng đoạn) và chọn vị trí, link trong danh sách
   link của trang; không được bịa link.
3. Trang chi tiết: JSON-LD JobPosting; không có thì parser chung (tiêu đề + nội dung chính +
   các trường có nhãn như "Mức lương:"); trang không rõ là tin tuyển dụng thì hỏi LLM, không
   có LLM thì bỏ. Trang "chi tiết" hoá ra là danh sách con thì lấy tiếp link tin trên đó.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlsplit

from jobcrawler.logging import get_logger
from jobcrawler.net.http import CapturedJson, FetchResult, WaitUntil
from jobcrawler.net.urls import host_of, normalize_url, path_segments, same_site
from jobcrawler.pages import (
    PAGE_PARAMS,
    Link,
    clean_text,
    extract_links,
    html_to_markdown,
    looks_like_spa_shell,
    main_node,
    meta_content,
    next_page_urls,
    node_text,
    page_number,
    parse,
)
from jobcrawler.prompts import job_detail, job_listing
from jobcrawler.schemas import ExtractionMethod, RawJob
from jobcrawler.sources import json_jobs, jsonld
from jobcrawler.sources.base import (
    Source,
    SourceError,
    SourceSession,
    gather_all,
    text_or_none,
    transient_failure,
)
from jobcrawler.text import strip_accents

log = get_logger(__name__)

KIND = "careers"
# Mô tả ngắn hơn mức này coi như JSON-LD của trang danh sách (chỉ có tiêu đề), cần trang chi tiết.
_MIN_DESCRIPTION = 200
_MAX_LLM_LINKS = 150
_MAX_LLM_LISTINGS = 2
_LLM_LISTING_CHUNK = 6_000  # ký tự markdown mỗi lời gọi
_LLM_LISTING_CHUNKS = 4
# Câu trả lời LLM theo nội dung trang (lượt refresh không hỏi lại trang không đổi).
LISTING_CACHE_SECONDS = 86_400
DETAIL_CACHE_SECONDS = 7 * 86_400

# Đoạn đường dẫn hay gặp của trang việc làm (đã bỏ dấu, viết thường).
JOB_SEGMENTS = frozenset(
    {
        "job",
        "jobs",
        "career",
        "careers",
        "tuyen-dung",
        "tuyendung",
        "viec-lam",
        "vieclam",
        "vi-tri",
        "vi-tri-tuyen-dung",
        "co-hoi-nghe-nghiep",
        "co-hoi-viec-lam",
        "tin-tuyen-dung",
        "chi-tiet-tuyen-dung",
        "job-detail",
        "job-details",
        "jobdetail",
        "position",
        "positions",
        "opening",
        "openings",
        "vacancy",
        "vacancies",
        "recruitment",
        "opportunities",
        "opportunity",
        "o",  # Recruitee/Teamtailor: /o/<slug>
        "jd",
    }
)
# Đoạn đường dẫn của trang không phải tin (danh mục, tin tức, đăng nhập...).
STOP_SEGMENTS = frozenset(
    {
        "page",
        "category",
        "categories",
        "danh-muc",
        "tag",
        "tags",
        "search",
        "tim-kiem",
        "department",
        "departments",
        "location",
        "locations",
        "login",
        "register",
        "dang-nhap",
        "dang-ky",
        "news",
        "tin-tuc",
        "blog",
        "blogs",
        "su-kien",
        "events",
        "press",
        "bao-chi",
        "media",
        "thu-vien",
        "gallery",
        "about",
        "about-us",
        "gioi-thieu",
        "lien-he",
        "contact",
        "faq",
        "privacy",
        "terms",
        "policy",
        "chinh-sach",
        "feed",
        "author",
        "wp-content",
        "wp-admin",
        "cdn-cgi",
    }
)
# Đoạn đường dẫn bắt đầu bằng các cụm này không phải tin: trang tìm kiếm / lọc
# (/tuyen-dung/tim-kiem-theo-hash-tag), chính sách, điều khoản (privacy-policy,
# chinh-sach-bao-mat), tin đã lưu. So cả cụm ở đầu đoạn, không so chuỗi con: "chuyen-vien-bao-
# mat-thong-tin", "loc-hoa-dau", "search-engine-optimization-specialist" là tên vị trí.
_STOP_HEADS = (
    "tim-kiem",
    "search-result",
    "hash-tag",
    "hashtag",
    "filter",
    "loc-theo",
    "privacy",
    "policy",
    "cookie",
    "terms",
    "dieu-khoan",
    "chinh-sach",
    "saved-jobs",
    "job-alert",
    "favorite",
    "wishlist",
)
# ... hoặc kết thúc bằng: cookie-policy, terms-of-use, data-privacy.
_STOP_TAILS = ("privacy", "policy", "terms", "terms-of-use", "cookies")
# Đoạn có mã số (tin "-12", "/744000153509469/"): là tin, không phải trang chính sách.
_HAS_ID = re.compile(r"(?:^|-)\d{2,}(?:-|$)")
# Trang mục lục việc làm (/en/jobs/, /careers/tuyen-dung/): đi tiếp như trang danh sách.
_INDEX_SEGMENTS = frozenset(
    {
        "jobs",
        "job",
        "careers",
        "career",
        "tuyen-dung",
        "viec-lam",
        "vi-tri-tuyen-dung",
        "co-hoi-nghe-nghiep",
        "co-hoi-viec-lam",
        "openings",
        "positions",
        "vacancies",
        "opportunities",
    }
)
# Tiền tố ngôn ngữ (/en/, /vi-vn/): trang danh sách /en/ vẫn là trang gốc.
_LOCALE = re.compile(r"^[a-z]{2}(-[a-z]{2})?$")
_FILE_EXT = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|jpe?g|png|gif|webp|svg|zip|rar|mp4)$", re.I)
# Chữ trên link không nói lên tên vị trí.
_GENERIC_TEXT = re.compile(
    r"^(xem( them| chi tiet| ngay)?|chi tiet|ung tuyen( ngay)?|nop (ho so|don|cv)|apply( now)?|"
    r"read more|view( details?| job| more)?|details?|more|learn more|see more|tim hieu them|"
    r"\W*)$"
)
_VIEW_ALL = re.compile(
    r"(xem|tat ca|toan bo).{0,20}(vi tri|viec lam|cong viec|co hoi|tin tuyen dung)|"
    r"(all|view|see|browse|explore|search).{0,15}(jobs|positions|openings|opportunities|roles)|"
    r"open (positions|roles)|current openings|vi tri dang tuyen|co hoi viec lam|"
    r"tim (cong )?viec"
)
# Chữ "xem tất cả" trơn: chỉ tính khi đường dẫn trông như trang việc làm (/alljobs, /careers).
_VIEW_ALL_SHORT = re.compile(r"^(xem tat ca|tat ca|view all|see all|show all|all)$")
_ID_PARAMS = frozenset({"id", "job", "jid", "post", "pid", "slug", "code"})
# Đoạn cuối của trang danh sách (không phải tin): /careers/all-jobs, /alljobs, /tim-viec.
LISTING_SEGMENTS = frozenset(
    {
        "all-jobs",
        "alljobs",
        "all",
        "job-list",
        "jobs-list",
        "job-listing",
        "search-jobs",
        "job-search",
        "tim-viec",
        "tim-kiem-viec-lam",
        "danh-sach-viec-lam",
        "danh-sach-tuyen-dung",
        # SAP SuccessFactors Career Site: /search/, /viewalljobs/
        "search",
        "viewalljobs",
        "jobsearch",
    }
)
# Trang "chi tiết" có từ ngần này link tin trở lên thì là danh sách con.
_SUB_LISTING_LINKS = 3
# Trang không có mã tin trong URL mà dẫn tới từ ngần này tin có mã: trang danh mục / theo tỉnh
# (vieclam.thegioididong.com/tuyen-dung/ban-hang-thu-ngan... → /tuyen-dung/nhan-vien-...-45).
_HUB_ID_LINKS = 3
# Trang tin mà nội dung chính ngắn hơn mức này (ký tự markdown): mô tả do JavaScript nạp sau
# (Tiki: HTML chỉ có tiêu đề + hạn nộp), mở lại bằng trình duyệt.
_THIN_DETAIL = 400

# Nhóm từ khoá của một bản mô tả công việc (đã bỏ dấu). Đếm số nhóm xuất hiện.
_JD_SIGNALS: tuple[tuple[str, ...], ...] = (
    ("mo ta cong viec", "job description", "about the role", "the role", "job summary"),
    ("yeu cau", "requirements", "qualifications", "what you need", "who you are"),
    ("quyen loi", "phuc loi", "che do dai ngo", "benefits", "what we offer", "perks"),
    ("trach nhiem", "responsibilities", "what you will do", "what you'll do", "nhiem vu"),
    ("han nop", "deadline", "ung tuyen", "nop ho so", "apply"),
    ("muc luong", "thu nhap", "salary", "compensation"),
    ("dia diem lam viec", "noi lam viec", "location", "work location"),
    ("kinh nghiem", "experience"),
)
_CLEAR_JD = 4  # ≥ số nhóm này: chắc chắn là tin tuyển dụng, không cần hỏi LLM

# Trường có nhãn trong nội dung ("Mức lương: 10 - 15 triệu"). Khoá → nhãn (đã bỏ dấu).
_LABELS: dict[str, tuple[str, ...]] = {
    "salary_raw": ("muc luong", "luong", "thu nhap", "salary"),
    "location": (
        "dia diem lam viec",
        "noi lam viec",
        "dia diem",
        "khu vuc",
        "work location",
        "location",
    ),
    "expires_raw": ("han nop ho so", "han nop", "han ung tuyen", "deadline", "closing date"),
    "experience_raw": ("kinh nghiem", "experience"),
    "employment_type": (
        "hinh thuc lam viec",
        "hinh thuc",
        "loai hinh",
        "employment type",
        "job type",
    ),
    "level": ("cap bac", "level", "seniority"),
}
_MD_MARKS = re.compile(r"\*\*|__|`")
_LABEL_LINE = re.compile(r"^[\s>*_\-#|]*(?P<label>[^:\n]{2,40}?)[\s*_]*[:：][\s*_]*(?P<value>.*)$")


@dataclass(frozen=True, slots=True)
class JobLink:
    url: str
    title: str | None  # chữ trên link nếu nói lên tên vị trí


def content_key(*parts: str) -> str:
    """Khoá cache LLM theo nội dung đã gửi."""
    return hashlib.sha256("".join(parts).encode()).hexdigest()[:32]


def _plain(text: str) -> str:
    return clean_text(strip_accents(text).lower())


def _title_like(text: str) -> bool:
    words = text.split()
    return 2 <= len(words) <= 25 and not _GENERIC_TEXT.match(_plain(text))


def _job_like(segment: str) -> bool:
    return segment in JOB_SEGMENTS or any(
        k in segment for k in ("job", "career", "tuyen-dung", "tuyendung", "viec-lam")
    )


def _segments(url: str) -> list[str]:
    return [
        strip_accents(s).lower().replace("_", "-").removesuffix(".html").removesuffix(".htm")
        for s in path_segments(url)
    ]


def _stop(segment: str) -> bool:
    if segment in STOP_SEGMENTS:
        return True
    if _HAS_ID.search(segment):
        return False
    heads = any(segment == h or segment.startswith(h + "-") for h in _STOP_HEADS)
    tails = any(segment == t or segment.endswith("-" + t) for t in _STOP_TAILS)
    return heads or tails


def _has_job_id(url: str) -> bool:
    """URL mang mã tin: đoạn cuối có số (`-45`, `/744000153509469/`) hoặc tham số `?id=`."""
    segs = _segments(url)
    if segs and _HAS_ID.search(segs[-1]):
        return True
    return any(_is_id_param(k.lower()) for k, _ in parse_qsl(urlsplit(url).query))


def hub_page(page_url: str, links: Sequence[JobLink]) -> bool:
    """Trang mở như tin nhưng là trang danh mục: URL không có mã tin, dẫn tới nhiều tin có mã.
    Trang tin thật có mã thì khối "việc làm khác" trên đó không làm nó thành danh mục."""
    if _has_job_id(page_url):
        return False
    return sum(1 for link in links if _has_job_id(link.url)) >= _HUB_ID_LINKS


def _content_segments(segs: list[str]) -> list[str]:
    """Bỏ tiền tố ngôn ngữ ở đầu đường dẫn."""
    i = 0
    while i < len(segs) and _LOCALE.match(segs[i]):
        i += 1
    return segs[i:]


def _is_index(segs: list[str]) -> bool:
    """/alljobs, /en/jobs/, /careers/tuyen-dung/: trang danh sách, không phải tin."""
    content = _content_segments(segs)
    if not content:
        return False
    return content[-1] in LISTING_SEGMENTS or (len(content) <= 2 and content[-1] in _INDEX_SEGMENTS)


def _is_id_param(key: str) -> bool:
    """`id`, `jobId`, `request_id`, `postid`... (không tính `pageId` của banner/tracking)."""
    if key.startswith(("page", "from", "utm", "ref", "source")):
        return False
    return key in _ID_PARAMS or key.endswith(("_id", "jobid", "postid", "requestid"))


def job_links(links: list[Link], listing_url: str) -> list[JobLink]:
    """Link có dạng trang chi tiết tin tuyển dụng, cùng site với trang danh sách."""
    listing = normalize_url(listing_url) or listing_url
    listing_segs = _segments(listing)
    # Trang danh sách là trang gốc (hoặc chỉ có tiền tố ngôn ngữ) thì mọi link đều "con".
    listing_is_root = not _content_segments(listing_segs)
    best: dict[str, JobLink] = {}
    for link in links:
        url = link.url
        if url == listing or not same_site(url, listing) or _FILE_EXT.search(urlsplit(url).path):
            continue
        segs = _segments(url)
        if not segs or segs[-1] in LISTING_SEGMENTS or any(_stop(s) for s in segs):
            continue
        query = {k.lower() for k, _ in parse_qsl(urlsplit(url).query)}
        if query & PAGE_PARAMS:
            continue
        by_id = any(_is_id_param(k) for k in query)  # /job-detail.html?id=12
        # Trang con của trang danh sách (/tuyen-dung → /tuyen-dung/ke-toan). Trang danh sách là
        # trang gốc thì mọi link đều "con", nên không tính.
        child = (
            not listing_is_root
            and host_of(url) == host_of(listing)
            and len(segs) > len(listing_segs)
            and segs[: len(listing_segs)] == listing_segs
        )
        parents = segs if by_id else segs[:-1]
        job_seg = any(s in JOB_SEGMENTS for s in parents) or any(
            s.startswith(("tuyen-dung-", "viec-lam-", "job-", "career-", "recruitment-"))
            for s in segs
        )
        last = segs[-1]
        slug = by_id or ((bool(re.search(r"\d", last)) or last.count("-") >= 2) and len(last) >= 5)
        titled = _title_like(link.text)
        # Tham số khác id (?dept=, ?location=) thường là bộ lọc của trang danh sách.
        filtered = bool(query) and not by_id
        score = (2 if child or job_seg else 0) + slug + titled - filtered
        if score < 3:
            continue
        title = link.text if titled else None
        current = best.get(url)
        if current is None or (title and len(title) > len(current.title or "")):
            best[url] = JobLink(url=url, title=title or (current.title if current else None))
    return list(best.values())


def _listing_like(link: Link, segs: list[str]) -> bool:
    """Trang mục lục việc làm (/tim-kiem-viec-lam, /en/jobs/, /search/) hoặc link "xem tất cả
    vị trí" (không phải trang chính sách, tin đã lưu...)."""
    if not urlsplit(link.url).query and _is_index(segs):
        return True
    if any(_stop(s) for s in segs):
        return False
    text = _plain(link.text)
    return bool(_VIEW_ALL.search(text)) or bool(
        _VIEW_ALL_SHORT.match(text) and any(_job_like(s) for s in segs)
    )


def listing_links(links: list[Link], page_url: str) -> list[str]:
    """Trang danh sách tiếp theo: phân trang của trang hiện tại, link "xem tất cả vị trí"."""
    pages = next_page_urls(links, page_url)
    others = [
        link.url
        for link in links
        if same_site(link.url, page_url)
        and link.url != page_url
        and page_number(link.url) is None
        and (segs := _segments(link.url))
        and _listing_like(link, segs)
    ]
    return list(dict.fromkeys([*pages, *others]))


def jd_signals(markdown: str) -> int:
    text = _plain(markdown)
    return sum(any(k in text for k in group) for group in _JD_SIGNALS)


def labeled_fields(markdown: str) -> dict[str, str]:
    """Các trường có nhãn rõ ràng ("Mức lương: ...", "Hạn nộp: ...") trong nội dung. Nhãn đứng
    riêng một dòng ("MỨC LƯƠNG:" rồi "Thỏa thuận") thì lấy dòng ngay sau."""
    found: dict[str, str] = {}
    lines = [line for line in markdown.splitlines() if line.strip()]
    for i, line in enumerate(lines):
        m = _LABEL_LINE.match(line)
        if m is None:
            continue
        label = _plain(m["label"]).strip(" *_")
        key = next((k for k, names in _LABELS.items() if label in names), None)
        if key is None or key in found:
            continue
        value = clean_text(_MD_MARKS.sub("", m["value"]).strip(" *|"))
        if not value and i + 1 < len(lines) and _LABEL_LINE.match(lines[i + 1]) is None:
            value = clean_text(_MD_MARKS.sub("", lines[i + 1]).strip(" *|#-"))
        if value and len(value) <= 200:
            found[key] = value
    return found


@dataclass(frozen=True, slots=True)
class Page:
    url: str  # URL cuối (sau redirect), đã chuẩn hoá
    html: str
    rendered: bool = False
    captured: tuple[CapturedJson, ...] = ()  # JSON trang gọi khi mở bằng trình duyệt


def _render_needed(result: FetchResult, html: str) -> bool:
    content_type = result.headers.get("content-type", "")
    return "html" in content_type and looks_like_spa_shell(html)


def _page(result: FetchResult, rendered: bool) -> Page:
    return Page(
        normalize_url(result.final_url) or result.final_url,
        result.text,
        rendered,
        result.captured,
    )


async def fetch_page(
    session: SourceSession,
    url: str,
    *,
    required: bool = False,
    render: bool = False,
    auto_render: bool = True,
    wait_until: WaitUntil = "domcontentloaded",
    capture_json: bool = False,
) -> Page | None:
    """Một trang HTML. `render=True` thì mở bằng trình duyệt; HTML là khung SPA thì cũng mở
    bằng trình duyệt (trừ khi `auto_render=False`) và chờ mạng yên vì nội dung nạp bằng XHR.

    `required`: lỗi thì dừng nguồn (`SourceError`) thay vì trả None. `capture_json`: khi mở bằng
    trình duyệt, giữ các phản hồi JSON trang gọi (danh sách tin của trang SPA).
    """
    if render:
        if not session.can_render:
            session.truncate("render_budget")
            return None
        result = await session.render(url, wait_until=wait_until, capture_json=capture_json)
        if not result.ok:
            if transient_failure(result):
                session.truncate("page_error")
            return None
        return _page(result, True)
    result = await (session.fetch_ok(url) if required else session.fetch(url))
    if not result.ok:
        if transient_failure(result):
            session.truncate("page_error")
        return None
    if auto_render and _render_needed(result, result.text):
        if not session.can_render:
            session.truncate("render_budget")
            return _page(result, False)
        rendered = await session.render(url, wait_until="networkidle", capture_json=capture_json)
        if rendered.ok:
            return _page(rendered, True)
    return _page(result, False)


@dataclass(slots=True)
class ListingScan:
    """Những gì đọc được trên một trang danh sách."""

    page: Page
    links: list[Link]
    jobs: list[RawJob]  # JobPosting JSON-LD / tin trong JSON của trang, đủ nội dung
    job_links: list[JobLink]
    next_pages: list[str]
    # API danh sách tin (hoặc trang có JSON nhúng) còn trang sau: (cách phân trang, số tin đã đọc).
    apis: list[tuple[json_jobs.Pager, int]] = field(default_factory=list)
    id_names: dict[tuple[str, str], str] = field(default_factory=dict)  # danh mục trang tải kèm

    @property
    def empty(self) -> bool:
        return not self.jobs and not self.job_links


def json_docs(page: Page) -> list[json_jobs.JsonDoc]:
    """JSON của trang: nhúng trong HTML (`__NEXT_DATA__`...) và phản hồi API trang gọi khi mở
    bằng trình duyệt."""
    docs = [
        json_jobs.JsonDoc(url=page.url, data=data, embedded=True)
        for data in json_jobs.embedded_json(page.html)
    ]
    for captured in page.captured:
        try:
            data = json.loads(captured.body)
        except ValueError:
            continue
        docs.append(
            json_jobs.JsonDoc(
                url=captured.url,
                data=data,
                method=captured.method,
                request_json=captured.request_json,
            )
        )
    return docs


def scan_listing(page: Page) -> ListingScan:
    links = extract_links(parse(page.html), page.url)
    jobs: list[RawJob] = []
    found = job_links(links, page.url)
    known = {j.url for j in found}
    for job in jsonld.raw_jobs(page.html, page.url):
        if len(job.description or "") >= _MIN_DESCRIPTION:
            jobs.append(job)
        elif job.url != page.url and job.url not in known:
            # JSON-LD của trang danh sách chỉ có tiêu đề: cần trang chi tiết.
            found.append(JobLink(url=job.url, title=job.title))
            known.add(job.url)
    # Trang dựng bằng JavaScript: tin nằm trong dữ liệu JSON của trang.
    texts = {link.url: link.text for link in links}
    ids = {job.external_id for job in jobs}
    apis: list[tuple[json_jobs.Pager, int]] = []
    docs = json_docs(page)
    names = json_jobs.id_names(docs)
    for doc in docs:
        arrays = json_jobs.job_arrays(doc)
        if arrays and (pager := json_jobs.pager_for(doc, arrays[0])) is not None:
            apis.append((pager, len(arrays[0].items)))
        extracted = json_jobs.extract(doc, page_url=page.url, links=texts, names=names)
        for job in extracted.jobs:
            if job.external_id not in ids:
                ids.add(job.external_id)
                jobs.append(job)
        for url, title in extracted.links:
            if url not in known and url != page.url:
                known.add(url)
                found.append(JobLink(url=url, title=title))
    return ListingScan(page, links, jobs, found, listing_links(links, page.url), apis, names)


async def scan_with_render(
    session: SourceSession, url: str, *, required: bool, render: bool = True
) -> ListingScan | None:
    """Đọc trang danh sách; HTTP không thấy tin nào thì mở lại bằng trình duyệt (danh sách do
    JavaScript nạp sau). `render=False`: chỉ dùng HTTP."""
    page = await fetch_page(session, url, required=required, auto_render=render, capture_json=True)
    if page is None:
        return None
    scan = scan_listing(page)
    if render and scan.empty and not page.rendered:
        # Danh sách nạp bằng XHR sau khi trang tải: chờ mạng yên.
        rendered = await fetch_page(
            session, url, render=True, wait_until="networkidle", capture_json=True
        )
        if rendered is not None:
            # DOM đã chạy JavaScript chứa ít nhất những gì HTML gốc có.
            return scan_listing(rendered)
    return scan


def split_markdown(markdown: str, size: int) -> list[str]:
    """Chia markdown thành các đoạn ≤ `size` ký tự, cắt ở ranh giới dòng."""
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for line in markdown.splitlines():
        if current and length + len(line) + 1 > size:
            chunks.append("\n".join(current))
            current, length = [], 0
        current.append(line[:size])
        length += min(len(line), size) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"\w+", _plain(text)))


# Chữ trên link không phải tên vị trí: tiêu đề chỉ gồm các từ này thì bỏ.
_GENERIC_TITLE = {
    "tuyen", "dung", "viec", "lam", "co", "hoi", "nghe", "nghiep", "chi", "tiet", "vi", "tri",
    "thong", "tin", "lien", "he", "job", "jobs", "career", "careers", "detail", "details",
}  # fmt: skip
# Nhiễu trên thẻ tin ở trang danh sách: nhãn, lương dạng chữ, hạn nộp.
_CARD_NOISE = re.compile(
    r"(?i)(?<!\w)(?:new|hot|gấp|mới)(?!\w)|hạn nộp(?: hồ sơ)?\s*:?\s*\d{1,2}/\d{1,2}/\d{2,4}"
    r"|th[oỏ]a thuận|cạnh tranh|negotiable"
)


def clean_card_text(text: str) -> str:
    """ "NEW Nhân viên Tester/QC Thỏa thuận Hà Nội Hạn nộp hồ sơ: 31/10/2026" → bỏ nhãn,
    lương dạng chữ, hạn nộp (tên tỉnh vẫn còn: không đoán được ranh giới)."""
    return " ".join(_CARD_NOISE.sub(" ", text).split()).strip(" -–|:")


def slug_hint(url: str) -> str | None:
    """Chữ trong đoạn cuối URL (`/chi-tiet/tester-1061` → "tester"), bỏ số/mã."""
    segs = path_segments(url)
    if not segs:
        return None
    words = re.split(r"[-_+.]+", re.sub(r"\.(html?|aspx|php)$", "", segs[-1]))
    kept = [w for w in words if w and not re.search(r"\d", w)]
    return " ".join(kept) or None


def _title_score(want: set[str], candidate: str) -> float:
    got = _tokens(candidate)
    if not got or not want or got <= _GENERIC_TITLE:
        return 0.0
    inter = len(want & got)
    jaccard = inter / len(want | got)
    # Tiêu đề trên trang nằm trọn trong chữ của thẻ tin (thẻ có thêm lương, nơi làm, hạn nộp).
    contained = inter / len(got) if inter >= 2 else 0.0
    return max(jaccard, contained)


def pick_title(
    hint: str | None,
    headings: list[str],
    og_title: str | None,
    *,
    extra: Sequence[str] = (),
    slug: str | None = None,
) -> str | None:
    """Tên vị trí của trang chi tiết.

    `<h1>` hay là banner chung của site ("TMA tự hào là nơi làm việc..."), nên khi có chữ trên
    link ở trang danh sách (`hint`) thì chọn tiêu đề trên trang giống nó nhất (kể cả h3–h5 và
    phần tử `*title*` trong `extra`); không có `hint` thì so với chữ trong URL (`slug`). Không
    tiêu đề nào giống: dùng `hint` đã bỏ nhiễu của thẻ, không có thì `<h1>` / og:title.
    """
    fallback = (headings[0] if headings else None) or og_title
    want_text = hint or slug
    if not want_text:
        return fallback
    want = _tokens(want_text)
    best, best_score = None, 0.0
    for candidate in [*headings, og_title or "", *extra]:
        score = _title_score(want, candidate)
        if score > best_score:
            best, best_score = candidate, score
    if best is not None and best_score >= 0.5:
        return best
    if hint:
        return clean_card_text(hint) or hint
    return fallback


def _llm_listing_candidates(scans: list[ListingScan]) -> list[ListingScan]:
    """Trang để LLM đọc khi rule không thấy tin: trang có URL kiểu danh sách trước (trang
    careers gốc thường chỉ giới thiệu), tối đa 2 trang."""

    def listing_like(scan: ListingScan) -> bool:
        segs = _segments(scan.page.url)
        return bool(segs) and (segs[-1] in LISTING_SEGMENTS or segs[-1] in JOB_SEGMENTS)

    return sorted(scans, key=lambda s: not listing_like(s))[:_MAX_LLM_LISTINGS]


class CareersSource(Source):
    kind = KIND

    def __init__(self, url: str) -> None:
        super().__init__(host_of(url), url)

    # --- API danh sách tin (trang SPA) ---

    async def _api_page(
        self, session: SourceSession, pager: json_jobs.Pager, step: int
    ) -> list[json_jobs.JsonDoc] | None:
        """JSON của trang thứ `step` sau trang đầu (None: lỗi, dừng đọc API này)."""
        url, body = pager.request(step)
        try:
            if pager.embedded:
                result = await session.fetch(url)
            else:
                # Chỉ header tối thiểu, không cookie/token: API công khai của trang careers.
                result = await session.fetch(
                    url,
                    method=pager.method,
                    json=body if pager.method != "GET" else None,
                    headers={"Accept": "application/json, text/plain, */*"},
                    use_cache=False,
                )
        except SourceError:
            return None  # bị chặn: giữ những gì đã đọc, không dừng cả nguồn
        if not result.ok:
            return None
        if pager.embedded:
            return [
                json_jobs.JsonDoc(url=url, data=data, embedded=True)
                for data in json_jobs.embedded_json(result.text)
            ]
        try:
            data = result.json()
        except ValueError:
            return None
        return [json_jobs.JsonDoc(url=url, data=data, method=pager.method, request_json=body)]

    async def _api_jobs(
        self,
        session: SourceSession,
        pager: json_jobs.Pager,
        first: int,
        page: Page,
        links: dict[str, str],
        seen: set[str],
        names: json_jobs.IdNames,
    ) -> AsyncIterator[RawJob | JobLink]:
        """Đọc các trang sau của API danh sách tin: tin có mô tả, hoặc link trang tin. `seen`: mã
        tin / URL đã có — trang không có gì mới (API bỏ qua tham số trang) thì dừng."""
        read = first
        for step in range(1, session.limits.max_api_pages):
            if pager.done(read):
                return
            docs = await self._api_page(session, pager, step)
            if docs is None:
                session.truncate("api_pages")
                return
            fresh = 0
            for doc in docs:
                arrays = json_jobs.job_arrays(doc)
                if not arrays:
                    continue
                read += len(arrays[0].items)
                pager = json_jobs.with_doc(pager, doc)
                extracted = json_jobs.extract(doc, page_url=page.url, links=links, names=names)
                for job in extracted.jobs:
                    if job.external_id not in seen:
                        seen.add(job.external_id)
                        fresh += 1
                        yield job
                for url, title in extracted.links:
                    if url not in seen:
                        seen.add(url)
                        fresh += 1
                        yield JobLink(url=url, title=title)
            log.debug("careers.api_page", url=pager.url, step=step, fresh=fresh, read=read)
            if fresh == 0:
                return  # trang rỗng / API bỏ qua tham số trang
        if not pager.done(read):
            session.truncate("api_pages")

    # --- danh sách bằng LLM ---

    async def _llm_listing(
        self, session: SourceSession, scan: ListingScan
    ) -> tuple[list[JobLink], list[RawJob]]:
        url = scan.page.url
        candidates = list(
            {
                link.url: link
                for link in scan.links
                if same_site(link.url, url) and link.url != url
            }.values()
        )[:_MAX_LLM_LINKS]
        node = main_node(parse(scan.page.html))
        markdown = html_to_markdown(node.html or "") if node is not None else ""
        if not markdown.strip():
            return [], []
        links = "\n".join(f"{i}. {c.text or '-'} — {c.url}" for i, c in enumerate(candidates))
        # Danh sách dài: mỗi lời gọi chỉ đọc một đoạn để kịp sinh JSON trong giới hạn thời gian.
        results = await gather_all(
            session.llm.cached(
                job_listing.PROMPT,
                job_listing.JobListing,
                key=content_key(url, chunk, links),
                ttl=LISTING_CACHE_SECONDS,
                url=url,
                page=chunk,
                links=links,
            )
            for chunk in split_markdown(markdown, _LLM_LISTING_CHUNK)[:_LLM_LISTING_CHUNKS]
        )
        items = [
            item
            for r in results
            if r.value is not None and r.value.is_job_listing
            for item in r.value.jobs
        ]
        found: list[JobLink] = []
        inline: list[RawJob] = []
        titles: set[str] = set()
        for item in items:
            title = clean_text(item.title)
            if not title or title in titles:
                continue
            titles.add(title)
            if item.link is not None and 0 <= item.link < len(candidates):
                found.append(JobLink(url=candidates[item.link].url, title=title))
                continue
            # Vị trí không có trang riêng: chỉ có tên (mô tả nằm trên trang danh sách).
            inline.append(
                RawJob(
                    source=KIND,
                    external_id=jsonld.external_id_for_url(url, title),
                    url=url,
                    title=title,
                    locations=[loc] if (loc := text_or_none(item.location)) else [],
                    method=ExtractionMethod.LLM,
                    extra={"inline": True},
                )
            )
        return found, inline

    # --- chi tiết ---

    async def _detail(
        self, session: SourceSession, link: JobLink
    ) -> tuple[RawJob | None, list[JobLink], bool]:
        """(tin ở trang chi tiết, link tin mới thấy trên trang, trang là danh mục?).

        Trang danh mục / theo tỉnh (URL không có mã tin, dẫn tới nhiều tin có mã): không phải tin,
        lấy link tin trên đó. Trang không rõ là tin mà có nhiều link tin thì có thể là danh sách
        con (theo phòng ban, địa điểm): lấy cả các link đó. Trang tin thật cũng hay có mục "vị trí
        khác" nên vẫn bóc tin như thường (LLM quyết định khi không rõ).
        """
        page = await fetch_page(session, link.url)
        if page is None:
            return None, [], False
        return await self._detail_page(session, link, page)

    async def _rendered_detail(
        self, session: SourceSession, link: JobLink, page: Page
    ) -> tuple[RawJob | None, list[JobLink], bool] | None:
        """Trang tin quá mỏng qua HTTP: mở lại bằng trình duyệt (None: không mở được)."""
        if page.rendered or not session.can_render:
            return None
        rendered = await fetch_page(
            session, link.url, render=True, wait_until="networkidle", capture_json=True
        )
        if rendered is None:
            return None
        return await self._detail_page(session, link, rendered)

    async def _detail_page(
        self, session: SourceSession, link: JobLink, page: Page
    ) -> tuple[RawJob | None, list[JobLink], bool]:
        if postings := jsonld.raw_jobs(page.html, page.url):
            job = postings[0]
            if not same_site(job.url, page.url):
                job = job.model_copy(update={"url": page.url})
            if len(job.description or "") < _MIN_DESCRIPTION and (
                again := await self._rendered_detail(session, link, page)
            ):
                return again
            return job, [], False
        sub = scan_listing(page)
        if hub_page(page.url, sub.job_links):
            return None, [*(JobLink(j.url, j.title) for j in sub.jobs), *sub.job_links], True
        tree = parse(page.html)
        og_title = meta_content(tree, "og:title")
        node = main_node(tree)
        if node is None:
            return None, [], False
        markdown = html_to_markdown(node.html or "")
        if len(markdown) < _THIN_DETAIL and (
            again := await self._rendered_detail(session, link, page)
        ):
            return again
        signals = jd_signals(markdown)
        more: list[JobLink] = []
        if signals < _CLEAR_JD and len(sub.job_links) >= _SUB_LISTING_LINKS:
            more = sub.job_links
        headings = [node_text(h) for h in node.css("h1, h2")]
        extra = [
            text
            for n in tree.css("h3, h4, h5, [class*=title]")[:40]
            if (text := node_text(n)) and len(text) <= 150
        ]
        title = pick_title(
            link.title,
            [h for h in headings if h],
            og_title,
            extra=extra,
            slug=slug_hint(page.url),
        )
        return await self._parse_detail(session, page, title, markdown, signals), more, False

    async def _parse_detail(
        self, session: SourceSession, page: Page, title: str | None, markdown: str, signals: int
    ) -> RawJob | None:
        fields = labeled_fields(markdown)
        method = ExtractionMethod.HEURISTIC
        detail: job_detail.JobDetail | None = None
        if signals < _CLEAR_JD:
            if not session.llm.enabled:
                return None
            result = await session.llm.cached(
                job_detail.PROMPT,
                job_detail.JobDetail,
                key=content_key(page.url, title or "", markdown),
                ttl=DETAIL_CACHE_SECONDS,
                url=page.url,
                title=title or "",
                page=markdown,
            )
            if result.value is None or not result.value.is_job_posting:
                return None
            detail = result.value
            method = ExtractionMethod.LLM
            title = clean_text(detail.title) or title
        if not title or len(markdown) < 50:
            return None
        location = fields.get("location")
        return RawJob(
            source=KIND,
            external_id=jsonld.external_id_for_url(page.url),
            url=page.url,
            title=title,
            company_name=detail.company_name if detail else None,
            description=markdown,
            description_format="markdown",
            locations=(detail.locations if detail and detail.locations else [])
            or ([location] if location else []),
            work_mode=detail.work_mode if detail else None,
            employment_type=(detail.employment_type if detail else None)
            or fields.get("employment_type"),
            level=(detail.level if detail else None) or fields.get("level"),
            salary_raw=(detail.salary if detail else None) or fields.get("salary_raw"),
            experience_raw=(detail.experience if detail else None) or fields.get("experience_raw"),
            expires_raw=(detail.deadline if detail else None) or fields.get("expires_raw"),
            posted_raw=detail.posted if detail else None,
            apply_url=page.url,
            method=method,
            extra={"jd_signals": signals},
        )

    # --- nguồn ---

    async def jobs(self, session: SourceSession) -> AsyncIterator[RawJob]:
        limits = session.limits
        queue = [normalize_url(self.url) or self.url]
        visited: set[str] = set()
        pending: dict[str, JobLink] = {}  # trang chi tiết cần mở, theo thứ tự gặp
        yielded: set[str] = set()  # external_id đã yield
        done_urls: set[str] = set()  # trang đã có đủ nội dung hoặc đã mở
        scans: list[ListingScan] = []

        def add_links(links: list[JobLink]) -> None:
            for link in links:
                if link.url in done_urls:
                    continue
                current = pending.get(link.url)
                if current is None or (link.title and not current.title):
                    pending[link.url] = link

        while queue and len(visited) < limits.max_listing_pages:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            scan = await scan_with_render(session, url, required=not scans)
            if scan is None:
                continue
            visited.add(scan.page.url)
            scans.append(scan)
            if scan.jobs:
                session.count(url, len(scan.jobs))
            for job in scan.jobs:
                if job.external_id not in yielded:
                    yielded.add(job.external_id)
                    done_urls.add(job.url)
                    yield job
            add_links(scan.job_links)
            queue.extend(u for u in scan.next_pages if u not in visited)
            log.debug("careers.listing", url=scan.page.url, jobs=len(scan.jobs), links=len(pending))
        if any(u not in visited for u in queue):
            session.truncate("listing_pages")

        # API danh sách tin của trang SPA: các trang sau.
        seen = {*yielded, *pending}
        for scan in scans:
            texts = {link.url: link.text for link in scan.links}
            for pager, first in scan.apis:
                async for item in self._api_jobs(
                    session, pager, first, scan.page, texts, seen, scan.id_names
                ):
                    if isinstance(item, JobLink):
                        add_links([item])
                    elif item.external_id not in yielded:
                        yielded.add(item.external_id)
                        done_urls.add(item.url)
                        yield item

        if scans and not pending and not yielded and session.llm.enabled:
            for scan in _llm_listing_candidates(scans):
                found, inline = await self._llm_listing(session, scan)
                add_links(found)
                for job in inline:
                    if job.external_id not in yielded:
                        yielded.add(job.external_id)
                        yield job
                if found or inline:
                    break

        def reuse(url: str) -> RawJob | None:
            """Tin đã tải ở lượt gần đây: dùng lại, không mở trang (không tính vào giới hạn)."""
            known = session.reuse(url=url)
            if known is not None:
                done_urls.add(url)
            return known

        opened = 0
        hubs = 0  # trang danh mục đã mở (không tính vào giới hạn trang tin)
        while pending and opened < limits.max_detail_pages:
            batch: list[JobLink] = []
            size = min(limits.detail_concurrency, limits.max_detail_pages - opened)
            while pending and len(batch) < size:
                url = next(iter(pending))
                link = pending.pop(url)
                if url in done_urls:
                    continue
                if (known := reuse(url)) is not None:
                    if known.external_id not in yielded:
                        yielded.add(known.external_id)
                        yield known
                    continue
                done_urls.add(url)
                batch.append(link)
            opened += len(batch)
            for link, (job, more, hub) in zip(
                batch, await gather_all(self._detail(session, link) for link in batch), strict=True
            ):
                add_links(more)
                if hub:
                    hubs += 1
                    if hubs <= limits.max_sub_listings:
                        opened -= 1
                    log.debug("careers.hub", url=link.url, links=len(more))
                    continue
                if job is not None:
                    session.count(link.url, 1)
                if job is not None and job.external_id not in yielded:
                    yielded.add(job.external_id)
                    yield job
        # Hết lượt mở trang: tin còn lại đã biết thì vẫn dùng lại được.
        for url in list(pending):
            known = reuse(url) if url not in done_urls else None
            if known is not None and known.external_id not in yielded:
                yielded.add(known.external_id)
                yield known
        if any(url not in done_urls for url in pending):
            session.truncate("detail_pages")
