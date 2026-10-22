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

Lượt build ghi bộ khung (`session.plan`, sources/plan.py): trang danh sách, trang phải mở bằng
trình duyệt, API danh sách tin, trang danh mục, link không phải tin. Lượt poll (`_poll`) chỉ đọc
lại đúng các trang / API đó — API gọi thẳng bằng HTTP thay cho mở trình duyệt — rồi chỉ mở trang
chi tiết của link chưa biết.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Collection, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from selectolax.lexbor import LexborNode

from jobcrawler.common.logging import get_logger
from jobcrawler.common.pages import (
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
    visible_text_length,
)
from jobcrawler.common.schemas import ExtractionMethod, FetchOutcome, RawJob
from jobcrawler.common.text import fold, legal_forms, strip_accents
from jobcrawler.common.urls import (
    host_of,
    normalize_url,
    path_segments,
    registrable_domain,
    same_site,
    strip_www,
)
from jobcrawler.pipeline import normalize, provinces
from jobcrawler.prompts import job_detail, job_listing
from jobcrawler.services.net.http import CapturedJson, FetchResult, WaitUntil
from jobcrawler.sources import json_jobs, jsonld
from jobcrawler.sources.base import (
    Source,
    SourceError,
    SourceSession,
    gather_all,
    text_or_none,
    transient_failure,
)
from jobcrawler.sources.plan import ApiSpec, SitemapSpec
from jobcrawler.sources.sitemaps import is_sitemap, parse_sitemap, sitemap_body

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
# Lượt poll (hạn 60 s) chờ một lời gọi LLM tối đa chừng này giây: LLM quá tải thì để lượt sau.
POLL_LLM_SECONDS = 15.0
# Lời gọi LLM quá giờ của lượt poll vẫn chạy nốt ở nền (giữ tham chiếu tới khi xong).
_FINISHING: set[asyncio.Future[Any]] = set()
# Sitemap của site careers: số file đọc mỗi lượt build (sitemap index → file con), số link tin
# lấy tối đa, và tuổi `lastmod` tối đa của một tin (cũ hơn: tin đã đóng mà sitemap còn giữ).
_MAX_SITEMAPS = 3
_MAX_SITEMAP_LINKS = 500
SITEMAP_MAX_AGE_DAYS = 180
# File con của sitemap index nên đọc trước.
_JOB_SITEMAP = re.compile(r"job|career|tuyen|recruit|viec|vacanc|position", re.I)

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
# Mã tin ở cuối đoạn cuối ("-16271", "/744000153509469"); năm trong slug bài viết
# ("tong-ket-nam-2024") không tính.
_TAIL_ID = re.compile(r"(?:^|-)(\d{2,})$")
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
_ID_PARAMS = frozenset(
    {
        "id",
        "job",
        "jid",
        "post",
        "pid",
        "slug",
        "code",
        "jobcode",
        "position",
        "positionid",
        "vacancy",
        "vacancyid",
    }
)
# Cụm tuyển dụng trong đoạn đường dẫn: "thong-bao-tuyen-dung-quan-ly-nha-an",
# "kbsv-tuyen-dung-chuyen-vien-kiem-thu", "cac-vi-tri-tuyen-dung".
_JOB_TOKEN = re.compile(r"(?:^|-)(?:tuyen-dung|tuyendung|recruitment|recruit|hiring)(?:-|$)")
_JOB_POST = re.compile(
    r"^(?:thong-bao-|tin-|[a-z0-9]+-)?(?:tuyen-dung|tuyendung|recruitment|hiring)-(?P<rest>.+)$"
)
# Từ sau cụm tuyển dụng không nói lên tên vị trí ("tuyen-dung-nhan-su-thang-10-2026").
_POST_FILLER = frozenset(
    {"thang", "nam", "dot", "nhan", "su", "cac", "vi", "tri", "nhieu", "moi", "nhat", "gap", "quy"}
)
# Phần trước tên vị trí trên thẻ tin: "[HN]", "(Gấp)", "HOT", "Tuyển dụng", "Cần tuyển 05".
_ROLE_PREFIX = re.compile(
    r"^(?:[\[(][^\])]{0,30}[\])]\s*|(?:hot|gap|new|moi|urgent)\b\W*"
    r"|(?:can )?tuyen(?: dung| gap)?\b\W*|\d{1,3}\s+)*"
)
# Danh từ chỉ vị trí trong chữ trên link / tiêu đề trang.
_ROLE = re.compile(
    r"\b(?:nhan vien|chuyen vien|truong phong|pho phong|truong nhom|truong ban|ky su|ke toan|"
    r"giam doc|thuc tap sinh|cong nhan|lao dong|tro ly|thu ky|giao vien|giang vien|"
    r"bac si|dieu duong|duoc si|lap trinh vien|ky thuat vien|tu van vien|tai xe|lai xe|bao ve|"
    r"le tan|developer|engineer|manager|specialist|executive|officer|intern|analyst|designer|"
    r"consultant|tester|accountant|director|leader|supervisor|coordinator|assistant)\b"
)
_EN_ROLE = re.compile(
    r"\b(?:engineer|developer|manager|specialist|executive|officer|intern|analyst|designer|"
    r"consultant|tester|accountant|director|lead|leader|supervisor|coordinator|assistant)s?$"
)
# Đuôi ngày (ddmmyyyy / yyyymmdd: "-28072026") trong slug bài viết: không phải mã tin.
_DATE_TAIL = re.compile(
    r"(?:^|-)(?:(?:0[1-9]|[12]\d|3[01])(?:0[1-9]|1[0-2])20\d\d"
    r"|20\d\d(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01]))$"
)
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
# Host của cổng tuyển dụng trên subdomain của chính site.
# Sàn tuyển dụng nhiều doanh nghiệp (mọi sàn có adapter `sources/boards` đều có ở đây).
JOB_BOARDS = frozenset(
    {
        "topcv.vn", "vietnamworks.com", "careerviet.vn", "careerbuilder.vn", "jobsgo.vn",
        "itviec.com", "vieclam24h.vn", "topdev.vn", "123job.vn", "glints.com", "careerlink.vn",
        "timviecnhanh.com", "timviec365.vn", "mywork.com.vn", "joboko.com", "vieclamtot.com",
        "linkedin.com", "indeed.com", "ybox.vn",
    }
)  # fmt: skip
# Trang hồ sơ nhà tuyển dụng trên sàn / chuyên trang việc làm (`/nha-tuyen-dung/<tên>`).
_EMPLOYER_SEGMENTS = frozenset(
    {"nha-tuyen-dung", "cong-ty", "companies", "company", "employer", "employers"}
)
# Trang có link tới chừng này hồ sơ nhà tuyển dụng (tên pháp nhân) khác nhau là trang của sàn.
MULTI_EMPLOYER_LINKS = 3


def job_board_host(url: str) -> bool:
    """URL trên chính trang tuyển dụng nhiều doanh nghiệp (`topcv.vn`, `www.itviec.com`; không tính
    subdomain riêng kiểu `tuyendung.<sàn>`): danh sách ở đó là tin của doanh nghiệp khác. Tin của
    chính công ty sở hữu sàn đến qua trang công ty của nó trên sàn (nguồn `board:`)."""
    host = strip_www(host_of(url))
    return host in JOB_BOARDS


def multi_employer_page(links: Sequence[Link]) -> bool:
    """Trang liệt kê tin của nhiều doanh nghiệp (sàn tuyển dụng, chuyên trang việc làm của báo):
    link tới ≥ MULTI_EMPLOYER_LINKS hồ sơ nhà tuyển dụng khác nhau mang tên pháp nhân. Trang
    tuyển dụng của một doanh nghiệp (kể cả tập đoàn kể tên công ty con) không có các trang đó."""
    names: set[str] = set()
    for link in links:
        segments = path_segments(link.url)
        if (
            len(segments) >= 2
            and any(s in _EMPLOYER_SEGMENTS for s in segments[:-1])
            and legal_forms(link.text)
        ):
            names.add(fold(link.text))
    return len(names) >= MULTI_EMPLOYER_LINKS


# Lỗi của nguồn careers mà trang chính là trang tin nhiều nhà tuyển dụng, hay mọi link tin của nó
# dẫn vào một trang như vậy: không phải nguồn của doanh nghiệp (lượt build không ghi bộ khung).
JOB_BOARD_PAGE = "job_board_page"


def _job_board_listing(url: str, scan: ListingScan) -> bool:
    """Trang danh sách thật ra là trang của sàn / chuyên trang việc làm (tới qua link "Việc làm"
    hay chuyển hướng): tin ở đó là của doanh nghiệp khác, không đọc."""
    if job_board_host(scan.page.url) or multi_employer_page(scan.links):
        log.info("careers.job_board_page", url=url, final_url=scan.page.url)
        return True
    return False


_PORTAL_HOSTS = ("careers.", "career.", "tuyendung.", "vieclam.", "jobs.", "job.", "recruit.")
# Trang "chi tiết" có từ ngần này link tin trở lên thì là danh sách con.
_SUB_LISTING_LINKS = 3
# Từ ngần này link có mã (`/<slug>-16271`) và chữ giống tên vị trí trở lên, cùng một thư mục cha:
# là các thẻ tin của danh sách, kể cả khi đường dẫn không có từ khoá tuyển dụng
# (tuyendung.frt.vn/quan-12-ho-chi-minh-duoc-si-ban-thuoc-11517). Cũng là số link tối thiểu để
# nhận một khuôn URL tin (`url_shape`) khi đọc sitemap.
_ID_SIBLINGS = 3
# Trang không có mã tin trong URL mà dẫn tới từ ngần này tin có mã: trang danh mục / theo tỉnh
# (vieclam.thegioididong.com/tuyen-dung/ban-hang-thu-ngan... → /tuyen-dung/nhan-vien-...-45).
_HUB_ID_LINKS = 3
# Trang tin mà nội dung chính ngắn hơn mức này (ký tự markdown): mô tả do JavaScript nạp sau
# (Tiki: HTML chỉ có tiêu đề + hạn nộp), mở lại bằng trình duyệt.
_THIN_DETAIL = 400

# Nhóm từ khoá của một bản mô tả công việc (đã bỏ dấu). Đếm số nhóm xuất hiện.
_JD_SIGNALS: tuple[tuple[str, ...], ...] = (
    (
        "mo ta cong viec",
        "chi tiet cong viec",
        "job description",
        "about the role",
        "the role",
        "job summary",
    ),
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
    "expires_raw": (
        "han nop ho so",
        "han nop",
        "han ung tuyen",
        "han nhan ho so",
        "thoi han nhan ho so",
        "thoi gian nhan ho so",
        "thoi han nop ho so",
        "han chot nop ho so",
        "han chot",
        "han cuoi",
        "han cuoi nop ho so",
        "han cuoi cung nop ho so",
        "ngay het han",
        "deadline",
        "closing date",
        "application deadline",
    ),
    "experience_raw": (
        "kinh nghiem",
        "experience",
        "years of experience",
        "years of experience required",
        "required experience",
    ),
    "employment_type": (
        "hinh thuc lam viec",
        "hinh thuc",
        "loai hinh cong viec",
        "loai hinh",
        "employment type",
        "job type",
    ),
    "level": ("cap bac", "level", "seniority", "job level", "career level"),
    "posted_raw": (
        "ngay dang",
        "ngay dang tin",
        "ngay dang tuyen",
        "ngay dang bai",
        "date posted",
        "posted on",
        "posted date",
    ),
    # Tên vị trí khi tiêu đề trang chỉ là "Thông báo tuyển dụng".
    "position": (
        "vi tri",
        "vi tri tuyen dung",
        "vi tri cong viec",
        "vi tri can tuyen",
        "chuc danh",
        "position",
        "job title",
    ),
    # Ghi vào `RawJob.extra` (normalize đọc `openings`, `education`).
    "openings": ("so luong can tuyen", "so luong tuyen", "so luong", "openings", "headcount"),
    "education": (
        "trinh do hoc van",
        "trinh do",
        "hoc van",
        "bang cap",
        "education",
        "education level",
        "required education level",
    ),
}
_EXTRA_LABELS = ("openings", "education")
# Phần tử có thể là tiêu đề mục JD (`normalize.jd_section`): khối nhỏ nhất chứa các tiêu đề này là
# phần mô tả của trang tin, không lấy menu, cột bên, tin liên quan, chân trang, khung chat của site
# không có thẻ `main` / `article`.
_HEADING_TAGS = "h1, h2, h3, h4, h5, h6, strong, b, dt, th, label, p, span, li, [class*=title]"
_MIN_JD_BLOCK = 300  # ký tự chữ: khối nhỏ hơn (thanh tab "Mô tả | Yêu cầu") thì lấy khối cha
_ALL_PLACES = re.compile(r"\b(tat ca|toan quoc|nationwide)\b")
_MD_MARKS = re.compile(r"\*\*|__|`")
# Chữ ngoài phần mô tả (thanh thông tin, cột tóm tắt) gửi kèm tin để normalize bù lương / kinh
# nghiệm / học vấn: tối đa chừng này ký tự.
_PAGE_CONTEXT = 4_000
_LABEL_LINE = re.compile(r"^[\s>*_\-#|]*(?P<label>[^:\n]{2,40}?)[\s*_]*[:：][\s*_]*(?P<value>.*)$")
# Dòng là link, hay đuôi của link nhiều dòng (cả thẻ tin khác bọc trong `<a>`: "[Kế toán\n\nHạn
# nộp: 30/09/2026](/tuyen-dung/ke-toan)"): không phải trường của tin đang đọc.
_LINK_LINE = re.compile(r"^[\s*_\-]*\[|^[^\[]*\]\(")
# Số thứ tự trước nhãn: "7. Hạn nộp hồ sơ", "1) Mức lương", "III. Địa điểm", "– Hạn nộp".
_LABEL_NUMBER = re.compile(r"^(?:(?:\d{1,2}|[ivx]{1,4}|[a-e])[.)]|[–—+])\s*")


@dataclass(frozen=True, slots=True)
class JobLink:
    url: str
    title: str | None  # chữ trên link nếu nói lên tên vị trí


def _finished(task: asyncio.Future[Any]) -> None:
    _FINISHING.discard(task)
    if not task.cancelled():
        task.exception()  # lỗi của lời gọi nền đã được LLMClient ghi log


async def poll_bounded[T](session: SourceSession, call: Awaitable[T]) -> T:
    """Chờ lời gọi LLM `call`. Lượt poll chờ tối đa POLL_LLM_SECONDS (ném `TimeoutError`) nhưng
    không huỷ lời gọi: câu trả lời vẫn vào cache theo nội dung trang, lượt poll sau (cùng nội dung)
    có ngay thay vì lại hết giờ — huỷ giữa chừng thì lúc LLM chậm, lượt nào cũng hết giờ."""
    if not session.polling:
        return await call
    task = asyncio.ensure_future(call)
    try:
        return await asyncio.wait_for(asyncio.shield(task), POLL_LLM_SECONDS)
    finally:
        if not task.done():
            _FINISHING.add(task)
            task.add_done_callback(_finished)


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
    return list(_segment_tuple(url))


@lru_cache(maxsize=1 << 16)
def _segment_tuple(url: str) -> tuple[str, ...]:
    # Mỗi link trên trang danh sách được tách đoạn nhiều lần khi chấm điểm: nhớ kết quả.
    return tuple(
        strip_accents(s).lower().replace("_", "-").removesuffix(".html").removesuffix(".htm")
        for s in path_segments(url)
    )


@lru_cache(maxsize=1 << 16)
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
    if segs and _HAS_ID.search(segs[-1]) and not _DATE_TAIL.search(segs[-1]):
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


def _locale_key(url: str) -> str:
    """URL bỏ tiền tố ngôn ngữ (/en/, /vi-vn/): các bản ngôn ngữ của một trang cùng khoá."""
    parts = urlsplit(url)
    return "/".join([host_of(url), *_content_segments(_segments(url))]) + "?" + parts.query


def _is_index(segs: list[str]) -> bool:
    """/alljobs, /en/jobs/, /careers/tuyen-dung/: trang danh sách, không phải tin."""
    content = _content_segments(segs)
    if not content:
        return False
    return content[-1] in LISTING_SEGMENTS or (len(content) <= 2 and content[-1] in _INDEX_SEGMENTS)


def detail_url(posting_url: str, page_url: str) -> str:
    """URL của tin ở trang chi tiết: `url` của JobPosting khi đúng là một trang tin, không thì
    URL trang. Nhiều site ghi `url` là trang chủ, trang danh sách hay site khác cho mọi tin: các
    tin sẽ chung một mã."""
    if posting_url == page_url:
        return page_url
    segs = _segments(posting_url)
    if (
        not same_site(posting_url, page_url)
        or not _content_segments(segs)
        or _is_index(segs)
        or (_has_job_id(page_url) and not _has_job_id(posting_url))
    ):
        return page_url
    return posting_url


def _is_id_param(key: str) -> bool:
    """`id`, `jobId`, `request_id`, `postid`... (không tính `pageId` của banner/tracking)."""
    if key.startswith(("page", "from", "utm", "ref", "source")):
        return False
    return key in _ID_PARAMS or key.endswith(("_id", "jobid", "postid", "requestid"))


def url_shape(url: str) -> str | None:
    """Khuôn URL của một tin: host + thư mục cha, khi đoạn cuối kết thúc bằng mã tin
    (`tuyendung.frt.vn/<slug>-16271` → "tuyendung.frt.vn") và URL không có tham số."""
    segs = _segments(url)
    if not segs or urlsplit(url).query or (m := _TAIL_ID.search(segs[-1])) is None:
        return None
    if len(m[1]) == 4 and 1990 <= int(m[1]) <= 2039:
        return None  # năm, không phải mã tin
    return "/".join([host_of(url), *segs[:-1]])


def url_shapes(urls: Iterable[str]) -> list[str]:
    """Khuôn URL tin có từ `_ID_SIBLINGS` link tin trở lên."""
    counts = Counter(shape for url in set(urls) if (shape := url_shape(url)))
    return [shape for shape, n in counts.items() if n >= _ID_SIBLINGS]


def _parent(url: str, segs: list[str]) -> str:
    return "/".join([host_of(url), *segs[:-1]])


def role_title(text: str) -> bool:
    """Chữ trên link là tên vị trí: mở đầu bằng danh từ chỉ vị trí ("[HN] Chuyên viên tư vấn",
    "Tuyển kỹ sư cầu đường") hay tên tiếng Anh ngắn kết thúc bằng nó ("Senior Software
    Engineer") — không phải "Lễ hội nhân viên 2025"."""
    plain = _ROLE_PREFIX.sub("", _plain(text))
    return bool(_ROLE.match(plain)) or (len(plain.split()) <= 8 and bool(_EN_ROLE.search(plain)))


def job_post_slug(segment: str) -> bool:
    """Slug của bài tuyển dụng một vị trí: cụm tuyển dụng ở đầu (sau "thong-bao-" / "tin-" hay
    một từ thương hiệu) rồi tên vị trí — "thong-bao-tuyen-dung-quan-ly-nha-an",
    "kbsv-tuyen-dung-chuyen-vien-kiem-thu". Không phải "tuyen-dung-thang-10" (thông báo chung),
    "quy-trinh-tuyen-dung-nhan-vien" (trang giới thiệu)."""
    m = _JOB_POST.match(segment)
    if m is None:
        return False
    return any(not w.isdigit() and w not in _POST_FILLER for w in m["rest"].split("-"))


def job_links(links: list[Link], listing_url: str) -> list[JobLink]:
    """Link có dạng trang chi tiết tin tuyển dụng, cùng site với trang danh sách."""
    listing = normalize_url(listing_url) or listing_url
    listing_segs = _segments(listing)
    # Trang danh sách là trang gốc (hoặc chỉ có tiền tố ngôn ngữ) thì mọi link đều "con".
    listing_is_root = not _content_segments(listing_segs)
    candidates: list[tuple[Link, list[str], set[str]]] = []
    for link in links:
        url = link.url
        if url == listing or not same_site(url, listing) or _FILE_EXT.search(urlsplit(url).path):
            continue
        segs = _segments(url)
        # Đoạn chung với trang danh sách nằm trong mục tin tức ("/tin-tuc/tuyen-dung" →
        # "/tin-tuc/tuyen-dung/ke-toan-12") không làm link thành tin tức.
        shared = 0
        if host_of(url) == host_of(listing):
            while shared < min(len(segs), len(listing_segs)) and (
                segs[shared] == listing_segs[shared]
            ):
                shared += 1
        if not segs or segs[-1] in LISTING_SEGMENTS:
            continue
        # Bài tuyển dụng một vị trí trong mục tin tức ("/tin-tuc/tuyen-dung-brand-marketing")
        # thắng đoạn dừng ở thư mục cha.
        if any(_stop(s) for s in segs[shared:]) and not job_post_slug(segs[-1]):
            continue
        query = {k.lower() for k, _ in parse_qsl(urlsplit(url).query)}
        if query & PAGE_PARAMS or page_number(url) is not None:
            continue
        if segs[-1].isdigit() and len(segs[-1]) <= 3 and segs[:-1] == listing_segs:
            continue  # /tin-tuyen-dung/2/: trang sau của danh sách
        candidates.append((link, segs, query))
    # Thẻ tin cùng khuôn URL (link có mã + chữ giống tên vị trí) lặp lại trên trang.
    families = set(url_shapes(link.url for link, _, _ in candidates if _title_like(link.text)))
    # ... hay ≥ 3 link cùng thư mục cha mà chữ là tên vị trí ("Chuyên viên kế toán", "Kỹ sư cầu
    # đường"): thẻ tin của site đặt tin ở gốc, URL không có mã cũng không có từ khoá tuyển dụng.
    roles = Counter(
        _parent(link.url, segs) for link, segs, _ in candidates if role_title(link.text)
    )
    best: dict[str, JobLink] = {}
    for link, segs, query in candidates:
        url = link.url
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
        job_seg = (
            any(s in JOB_SEGMENTS or _JOB_TOKEN.search(s) for s in parents)
            or any(
                s.startswith(("tuyen-dung-", "viec-lam-", "job-", "career-", "recruitment-"))
                for s in segs
            )
            or job_post_slug(segs[-1])
        )
        last = segs[-1]
        slug = by_id or ((bool(re.search(r"\d", last)) or last.count("-") >= 2) and len(last) >= 5)
        titled = _title_like(link.text)
        sibling = url_shape(url) in families
        role = role_title(link.text) and roles[_parent(url, segs)] >= _ID_SIBLINGS
        # Tham số khác id (?dept=, ?location=) thường là bộ lọc của trang danh sách.
        filtered = bool(query) and not by_id
        score = (2 if child or job_seg else 0) + slug + titled + sibling + role - filtered
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
    # Trang careers giới thiệu, tin nằm ở cổng tuyển dụng trên subdomain riêng
    # (mbbank.com.vn/tuyen-dung → careers.mbbank.com.vn, msb.com.vn → jobs.msb.com.vn).
    portals = [
        link.url
        for link in links
        if same_site(link.url, page_url)
        and host_of(link.url) != host_of(page_url)
        and host_of(link.url).startswith(_PORTAL_HOSTS)
        and not any(_stop(s) for s in _segments(link.url))
    ]
    return list(dict.fromkeys([*pages, *others, *portals]))


def jd_signals(markdown: str) -> int:
    text = _plain(markdown)
    return sum(any(k in text for k in group) for group in _JD_SIGNALS)


def _label_key(label: str) -> str | None:
    plain = _plain(_MD_MARKS.sub("", label)).strip(" *_#|-")
    plain = _LABEL_NUMBER.sub("", plain)
    return next((k for k, names in _LABELS.items() if plain in names), None)


def _value_line(line: str) -> str | None:
    """Dòng ngay sau một nhãn đứng riêng là giá trị của nhãn, trừ khi nó là nhãn khác, dòng có nhãn
    hay tiêu đề mục ("Địa điểm" rồi "## Mô tả công việc" không có giá trị)."""
    if line.lstrip().startswith("#") or _LABEL_LINE.match(line) or _label_key(line) is not None:
        return None
    value = clean_text(_MD_MARKS.sub("", line).strip(" *|#-"))
    return value if value and normalize.jd_section(value) is None else None


def labeled_fields(markdown: str) -> dict[str, str]:
    """Các trường có nhãn rõ ràng ("Mức lương: ...", "Hạn nộp: ...") trong nội dung. Nhãn đứng
    riêng một dòng ("MỨC LƯƠNG:" hay "Mức lương" không có dấu hai chấm, như thanh thông tin của
    nhiều trang tin) thì lấy dòng ngay sau. Bỏ dòng là link (thẻ tin khác) và mọi thứ từ khối
    "Việc làm liên quan" trở đi; tin ghi nhiều hạn nộp (mỗi đợt / mỗi vị trí) thì giữ cả, normalize
    lấy ngày muộn nhất."""
    found: dict[str, str] = {}
    lines = [line for line in markdown.splitlines() if line.strip()]
    for i, line in enumerate(lines):
        if normalize.cutoff_heading(line):
            break
        if _LINK_LINE.match(line):
            continue
        m = _LABEL_LINE.match(line)
        key = _label_key(m["label"] if m is not None else line)
        if key is None or (key in found and key != "expires_raw"):
            continue
        value = clean_text(_MD_MARKS.sub("", m["value"]).strip(" *|")) if m is not None else ""
        if not value and i + 1 < len(lines):
            value = _value_line(lines[i + 1]) or ""
        if not value or len(value) > 200:
            continue
        if key in found:
            if value not in found[key]:
                found[key] = f"{found[key]}; {value}"
        else:
            found[key] = value
    return found


def page_context(markdown: str, description: str) -> str:
    """Chữ trong nội dung chính ngoài phần mô tả (thanh thông tin, cột tóm tắt), cắt ngắn."""
    rest = markdown.replace(description, "\n") if description in markdown else markdown
    return rest.strip()[:_PAGE_CONTEXT]


def _ancestors(node: LexborNode) -> list[LexborNode]:
    """Các nút từ gốc tài liệu xuống tới `node`."""
    chain: list[LexborNode] = []
    current: LexborNode | None = node
    while current is not None:
        chain.append(current)
        current = current.parent
    return chain[::-1]


def jd_block(node: LexborNode) -> LexborNode:
    """Khối nhỏ nhất trong `node` (nội dung chính) chứa mọi tiêu đề mục JD của trang (mô tả, yêu
    cầu, quyền lợi — như `normalize.split_sections`), khi trang có từ hai loại mục trở lên; không
    thì chính `node`. Tiêu đề mục nằm cả trong menu / cột bên thì khối đó là cả `node`, như cũ."""
    headings: list[LexborNode] = []
    sections: set[str] = set()
    for element in node.css(_HEADING_TAGS):
        text = node_text(element)
        if len(text) <= 80 and (section := normalize.jd_section(text)) is not None:
            headings.append(element)
            sections.add(section)
    if len(sections) < 2:
        return node
    block = node
    for level in zip(*(_ancestors(h) for h in headings), strict=False):
        if any(n != level[0] for n in level):
            break
        block = level[0]
    while block != node and len(node_text(block)) < _MIN_JD_BLOCK and block.parent is not None:
        block = block.parent
    return block if node in _ancestors(block) else node


def page_jd(html: str) -> str | None:
    """Khối mô tả công việc trong HTML trang tin (markdown), khi trang có rõ các mục JD."""
    node = main_node(parse(html))
    if node is None:
        return None
    block = jd_block(node)
    markdown = html_to_markdown(block.html or "")
    signals = jd_signals(markdown)
    if len(markdown) < _MIN_DESCRIPTION or signals < (2 if block != node else _CLEAR_JD):
        return None
    return markdown


def _thin_posting(job: RawJob) -> bool:
    """JobPosting chỉ ghi tóm tắt (một dòng) thay cho mô tả công việc."""
    parts = [p for p in (job.description, job.requirements) if p]
    if job.description_format == "html":
        parts = [html_to_markdown(p) for p in parts]
    return len(clean_text(" ".join(parts))) < _MIN_DESCRIPTION


def self_places(links: Sequence[Link], page_url: str) -> list[str]:
    """Tỉnh/thành ghi trên thẻ tin trỏ về chính trang: khối "tin liên quan" hay liệt kê cả tin
    đang xem kèm nơi làm (tuyendung.frt.vn). Dùng khi trang không có nhãn địa điểm."""
    texts = [link.text for link in links if link.url == page_url and link.text]
    if any(_ALL_PLACES.search(_plain(text)) for text in texts):
        return []  # tuyển ở mọi nơi ("Tất cả"): không kê vài tỉnh đầu của khối
    return list(dict.fromkeys(p for text in texts for p in provinces.find_provinces(text)))


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
    # Phản hồi JSON bắt được (không nhúng) có tin nhưng không phân trang.
    feeds: list[json_jobs.JsonDoc] = field(default_factory=list)
    # Tin / link tin trên trang (DOM, JSON-LD, JSON nhúng) mà các API bắt được không có: lượt
    # poll chỉ được thay trang này bằng API khi số này là 0.
    uncovered: int = 0
    # URL / mã tin chỉ có trong feed không phân trang (vd. "việc làm nổi bật", có thể xoay vòng):
    # vắng ở lượt poll không có nghĩa là đã gỡ.
    feed_only: set[str] = field(default_factory=set)

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
    on_page = {*known, *(job.url for job in jobs)}  # tin thấy trên chính trang (DOM, JSON-LD)
    from_api: set[str] = set()  # tin / link tin đến từ phản hồi API bắt được
    from_feed: set[str] = set()  # ... từ phản hồi không phân trang (URL và mã tin)
    from_list: set[str] = set()  # ... từ API có phân trang / JSON nhúng (URL và mã tin)
    apis: list[tuple[json_jobs.Pager, int]] = []
    feeds: list[json_jobs.JsonDoc] = []
    docs = json_docs(page)
    names = json_jobs.id_names(docs)
    for doc in docs:
        arrays = json_jobs.job_arrays(doc)
        pager = json_jobs.pager_for(doc, arrays[0]) if arrays else None
        if pager is not None:
            apis.append((pager, len(arrays[0].items)))
        extracted = json_jobs.extract(doc, page_url=page.url, links=texts, names=names)
        if not doc.embedded and pager is None and (extracted.jobs or extracted.links):
            feeds.append(doc)
        urls = [job.url for job in extracted.jobs] + [url for url, _ in extracted.links]
        (on_page if doc.embedded else from_api).update(urls)
        keys = {*urls, *(job.external_id for job in extracted.jobs)}
        is_feed = not doc.embedded and pager is None
        (from_feed if is_feed else from_list).update(keys)
        for job in extracted.jobs:
            if job.external_id not in ids:
                ids.add(job.external_id)
                jobs.append(job)
        for url, title in extracted.links:
            if url not in known and url != page.url:
                known.add(url)
                found.append(JobLink(url=url, title=title))
    return ListingScan(
        page,
        links,
        jobs,
        found,
        listing_links(links, page.url),
        apis,
        names,
        feeds,
        uncovered=len(on_page - from_api - {page.url}),
        feed_only=from_feed - from_list - on_page,
    )


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
            again = scan_listing(rendered)
            # Trình duyệt không thêm gì (trang hiện không có tin, không do JavaScript nạp): giữ
            # bản HTTP, bộ khung không ghi trang là `rendered` và lượt poll không mở lại bằng
            # trình duyệt mỗi lượt.
            return again if render_adds(scan, again) else scan
    return scan


# Trình duyệt thêm chừng này link trở lên (so với HTML gốc) thì trang có nội dung do JavaScript nạp.
_RENDER_NEW_LINKS = 3
# ... hoặc chữ hiện trên trang nhiều hơn chừng này lần (+ _RENDER_MORE_CHARS ký tự): danh sách tin
# chỉ có chữ, không có link (LLM đọc ở bước `llm_listing`).
_RENDER_MORE_TEXT = 1.25
_RENDER_MORE_CHARS = 200


def render_adds(http: ListingScan, rendered: ListingScan) -> bool:
    """Bản mở bằng trình duyệt có thêm gì so với bản HTTP (cùng URL, bản HTTP không thấy tin)."""
    if not rendered.empty or rendered.apis or rendered.feeds:
        return True
    new_links = {link.url for link in rendered.links} - {link.url for link in http.links}
    if len(new_links) >= _RENDER_NEW_LINKS:
        return True
    before = visible_text_length(parse(http.page.html))
    after = visible_text_length(parse(rendered.page.html))
    return after >= before * _RENDER_MORE_TEXT + _RENDER_MORE_CHARS


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


# Từ của tiêu đề kiểu thông báo chung ("THÔNG BÁO TUYỂN DỤNG", "VietABank - Tuyển dụng", "Tuyển
# dụng nhân sự tháng 10/2026"): không phải tên vị trí.
_ANNOUNCEMENT = _GENERIC_TITLE | {
    "thong", "bao", "can", "gap", "nhan", "su", "vien", "lao", "dong", "thang", "dot", "nam",
    "moi", "nhat", "recruitment", "hiring", "we", "are", "announcement", "trang", "chu",
}  # fmt: skip


def generic_title(title: str, page_url: str) -> bool:
    """Tiêu đề chỉ là thông báo tuyển dụng chung (bỏ tên thương hiệu của site)."""
    brand = registrable_domain(host_of(page_url)).split(".")[0]
    words = {w for w in _tokens(title) if not w.isdigit() and w != brand}
    return words <= _ANNOUNCEMENT


_MAX_INLINE_LINES = 150
_TABLE_RULE = re.compile(r"^\|?\s*:?-{2,}")


def _cells(line: str) -> list[str]:
    return [clean_text(_MD_MARKS.sub("", c)) for c in line.strip().strip("|").split("|")]


def table_fields(markdown: str, title: str) -> dict[str, str]:
    """Trường của vị trí ghi trên dòng bảng chứa tên nó, theo tiêu đề cột ("| Vị trí | Số lượng |
    Ngày hết hạn |")."""
    key = _plain(title)
    header: list[str] = []
    lines = markdown.split("\n")
    for i, line in enumerate(lines):
        if not line.lstrip().startswith("|"):
            header = []
            continue
        if i + 1 < len(lines) and _TABLE_RULE.match(lines[i + 1].strip()):
            header = _cells(line)
            continue
        if not header or not key or key not in _plain(line):
            continue
        found: dict[str, str] = {}
        for label, value in zip(header, _cells(line), strict=False):
            if value and (field := _label_key(label)) is not None:
                found.setdefault(field, value)
        return found
    return {}


def inline_sections(markdown: str, titles: Sequence[str]) -> dict[str, str]:
    """Mô tả của các vị trí không có trang riêng (cả JD nằm trên trang danh sách: từng mục, khối
    thu gọn, cửa sổ bật lên): phần từ dòng ghi tên vị trí tới dòng ghi tên một vị trí khác. Tên
    có thể xuất hiện nhiều lần (bảng tóm tắt đầu trang rồi từng mục): lấy lần có nhiều chữ theo
    sau nhất."""
    lines = markdown.split("\n")
    plain = [_plain(_MD_MARKS.sub("", line)).strip(" #*|-") for line in lines]
    hits: list[tuple[int, str]] = []
    for title in titles:
        key = _plain(title)
        if not key:
            continue
        hits += [
            (i, title) for i, line in enumerate(plain) if key in line and len(line) <= len(key) + 60
        ]
    starts = sorted({i for i, _ in hits})
    found: dict[str, str] = {}
    for i, title in hits:
        end = next((s for s in starts if s > i), len(lines))
        body = lines[i + 1 : min(end, i + 1 + _MAX_INLINE_LINES)]
        cut = next((k for k, line in enumerate(body) if normalize.cutoff_heading(line)), len(body))
        text = "\n".join(body[:cut]).strip()
        if len(text) > len(found.get(title, "")):
            found[title] = text
    return found


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


def api_spec(pager: json_jobs.Pager, listing: str) -> ApiSpec:
    return ApiSpec(
        url=pager.url,
        method=pager.method,
        body=pager.body,
        key=pager.key,
        kind=pager.kind,
        current=pager.current,
        size=pager.size,
        total=pager.total,
        in_body=pager.in_body,
        embedded=pager.embedded,
        listing=listing,
    )


def feed_spec(doc: json_jobs.JsonDoc, listing: str) -> ApiSpec:
    """Phản hồi JSON có tin nhưng không phân trang: poll gọi lại đúng request đó."""
    return ApiSpec(
        url=doc.url,
        method=doc.method,
        body=doc.request_json,
        paged=False,
        listing=listing,
    )


def sitemap_links(
    text: str, shapes: Collection[str], *, now: datetime | None = None
) -> list[JobLink]:
    """Link tin trong một sitemap `urlset`: URL cùng khuôn với link tin đã thấy trên trang danh
    sách (`url_shape`), bỏ URL có `lastmod` quá cũ (sitemap hay giữ cả tin đã đóng)."""
    _, items = parse_sitemap(text)
    oldest = (now or datetime.now(UTC)) - timedelta(days=SITEMAP_MAX_AGE_DAYS)
    found: dict[str, JobLink] = {}
    for loc, lastmod in items:
        url = normalize_url(loc)
        if url is None or url_shape(url) not in shapes or (lastmod and lastmod < oldest):
            continue
        found.setdefault(url, JobLink(url=url, title=None))
        if len(found) >= _MAX_SITEMAP_LINKS:
            break
    return list(found.values())


def _plan_request_failed(
    session: SourceSession, url: str, *, kind: str, wrong_type: str, truncation: str
) -> None:
    """Request theo bộ khung (API, sitemap) không trả dữ liệu: 4xx (trừ 429) hoặc trả về sai
    loại thì bộ khung hỏng (build lại); lỗi tạm thì chỉ đánh dấu lượt chưa đọc trọn."""
    last = session.records[-1] if session.records else None
    status = last.http_status if last is not None else None
    if status is not None and 400 <= status < 500 and status != 429:
        session.mark_broken(f"{kind}_http_{status}: {url}")
    elif last is not None and last.outcome is FetchOutcome.OK:
        session.mark_broken(f"{wrong_type}: {url}")
    else:
        session.truncate(truncation)


def api_pager(api: ApiSpec) -> json_jobs.Pager:
    return json_jobs.Pager(
        url=api.url,
        method=api.method,
        body=api.body,
        key=api.key,
        kind=api.kind,
        current=api.current,
        size=api.size,
        total=None,  # tổng số tin đọc lại từ phản hồi lúc poll
        in_body=api.in_body,
        embedded=api.embedded,
    )


@dataclass(slots=True)
class PageBudget:
    """Số trang (danh sách, danh mục, trang API) lượt poll còn được đọc ở một nguồn — phần của
    nguồn trong bộ khung. `limited=False`: không giới hạn. `cut`: đã phải bỏ trang vì hết."""

    left: int
    limited: bool
    cut: bool = False

    @classmethod
    def of(cls, pages: int) -> PageBudget:
        return cls(left=pages, limited=pages > 0)

    def take(self) -> bool:
        if not self.limited:
            return True
        if self.left <= 0:
            self.cut = True
            return False
        self.left -= 1
        return True


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
        budget: PageBudget | None = None,
    ) -> AsyncIterator[RawJob | JobLink]:
        """Đọc các trang sau của API danh sách tin: tin có mô tả, hoặc link trang tin. `seen`: mã
        tin / URL đã có — trang không có gì mới (API bỏ qua tham số trang) thì dừng. `budget`:
        số trang lượt poll còn được đọc (hết thì dừng, không tính là lỗi)."""
        read = first
        for step in range(1, session.limits.max_api_pages):
            if pager.done(read):
                return
            if budget is not None and not budget.take():
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

    # --- sitemap ---

    async def _sitemap_text(self, session: SourceSession, url: str) -> str | None:
        """Nội dung một sitemap (None: lỗi, bị chặn, hoặc trả về không phải sitemap)."""
        try:
            result = await session.fetch(url)
        except SourceError:
            return None
        if not result.ok:
            return None
        body = sitemap_body(result.content)
        return body if body is not None and is_sitemap(body) else None

    async def _sitemaps(
        self, session: SourceSession, shapes: Collection[str]
    ) -> list[tuple[SitemapSpec, list[JobLink]]]:
        """Sitemap của site careers có link tin cùng khuôn `shapes`: `/sitemap.xml`, không có thì
        `/sitemap_index.xml`; sitemap index thì đọc các file con tên giống tin tuyển dụng trước."""
        if not shapes:
            return []
        parts = urlsplit(self.url)
        root = f"{parts.scheme}://{parts.netloc}"
        queue = [f"{root}/sitemap.xml", f"{root}/sitemap_index.xml"]
        found: list[tuple[SitemapSpec, list[JobLink]]] = []
        read = 0
        while queue and read < _MAX_SITEMAPS:
            url = queue.pop(0)
            read += 1
            text = await self._sitemap_text(session, url)
            if text is None:
                continue
            queue = [u for u in queue if not u.endswith("/sitemap_index.xml")]
            is_index, items = parse_sitemap(text)
            if is_index:
                children = [loc for loc, _ in items if same_site(loc, root)]
                children.sort(key=lambda u: not _JOB_SITEMAP.search(u))
                queue = [*children, *queue]
                continue
            if links := sitemap_links(text, shapes):
                found.append((SitemapSpec(url=url, shapes=sorted(shapes)), links))
        return found

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
        try:
            results = await poll_bounded(
                session,
                gather_all(
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
                ),
            )
        except TimeoutError:
            # Lượt poll: LLM quá tải thì để lượt sau (không coi là danh sách trống / bộ khung hỏng).
            session.truncate("llm_listing")
            return [], []
        items = [
            item
            for r in results
            if r.value is not None and r.value.is_job_listing
            for item in r.value.jobs
        ]
        found: list[JobLink] = []
        inline: list[RawJob] = []
        titles: set[str] = set()
        sections = inline_sections(markdown, [clean_text(item.title) for item in items])
        for item in items:
            title = clean_text(item.title)
            if not title or title in titles:
                continue
            titles.add(title)
            if item.link is not None and 0 <= item.link < len(candidates):
                found.append(JobLink(url=candidates[item.link].url, title=title))
                continue
            # Vị trí không có trang riêng: mô tả là phần của nó trên trang danh sách.
            section = sections.get(title, "")
            row = table_fields(markdown, title)
            fields = {**row, **labeled_fields(section)}
            location = text_or_none(item.location) or fields.get("location")
            deadlines = [
                d
                for d in (
                    text_or_none(item.deadline),
                    row.get("expires_raw"),
                    fields.get("expires_raw"),
                )
                if d
            ]
            extra: dict[str, object] = {"inline": True, "jd_signals": jd_signals(section)}
            extra.update((k, fields[k]) for k in _EXTRA_LABELS if k in fields)
            inline.append(
                RawJob(
                    source=KIND,
                    external_id=jsonld.external_id_for_url(url, title),
                    url=url,
                    title=title,
                    description=section or None,
                    description_format="markdown",
                    locations=[location] if location else [],
                    employment_type=fields.get("employment_type"),
                    level=fields.get("level"),
                    salary_raw=fields.get("salary_raw"),
                    experience_raw=fields.get("experience_raw"),
                    expires_raw="; ".join(dict.fromkeys(deadlines)) or None,
                    method=ExtractionMethod.LLM,
                    extra=extra,
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
        job, more, hub = await self._detail_page(session, link, page)
        if job is None and not hub and not more and not session.polling:
            # Mở được mà không phải tin: lượt poll không mở lại link này.
            session.plan.add("ignored", link.url)
        return job, more, hub

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
            # Trang chi tiết là một tin: mã theo URL của tin (như `_parse_detail`).
            url = detail_url(postings[0].url, page.url)
            job = postings[0].model_copy(
                update={"url": url, "external_id": jsonld.external_id_for_url(url)}
            )
            if _thin_posting(job):
                if (full := page_jd(page.html)) is not None:
                    # JSON-LD chỉ ghi tóm tắt một dòng: mô tả đầy đủ là khối JD trong HTML.
                    job = job.model_copy(
                        update={
                            "description": full,
                            "description_format": "markdown",
                            "requirements": None,
                            "benefits": None,
                        }
                    )
                elif again := await self._rendered_detail(session, link, page):
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
        if title is None or generic_title(title, page.url):
            # "THÔNG BÁO TUYỂN DỤNG": tên vị trí nằm ở nhãn "Vị trí tuyển dụng: ..." trong bài.
            title = labeled_fields(markdown).get("position") or title
        # Mô tả: khối các mục JD (bỏ menu, cột bên, tin liên quan, chân trang); trường có nhãn,
        # dấu hiệu JD và LLM vẫn đọc cả nội dung chính.
        block = jd_block(node)
        description = markdown if block == node else html_to_markdown(block.html or "")
        job = await self._parse_detail(
            session,
            page,
            title,
            markdown,
            signals,
            description=description,
            places=self_places(sub.links, page.url),
            published=meta_content(tree, "article:published_time"),
        )
        return job, more, False

    async def _parse_detail(
        self,
        session: SourceSession,
        page: Page,
        title: str | None,
        markdown: str,
        signals: int,
        *,
        description: str | None = None,
        places: Sequence[str] = (),
        published: str | None = None,
    ) -> RawJob | None:
        fields = labeled_fields(markdown)
        method = ExtractionMethod.HEURISTIC
        detail: job_detail.JobDetail | None = None
        if signals < _CLEAR_JD:
            if not session.llm.enabled:
                return None
            if signals <= 1 and not _ROLE.search(_plain(title or "")):
                # Gần như chắc không phải tin (bài blog, trang menu, trang lọc): không tốn lượt
                # LLM — LLM dùng chung, mỗi lời gọi chờ có khi hàng chục giây.
                return None
            try:
                result = await poll_bounded(
                    session,
                    session.llm.cached(
                        job_detail.PROMPT,
                        job_detail.JobDetail,
                        key=content_key(page.url, title or "", markdown),
                        ttl=DETAIL_CACHE_SECONDS,
                        url=page.url,
                        title=title or "",
                        page=markdown,
                    ),
                )
            except TimeoutError:
                # Lượt poll không chờ LLM quá tải (hết giờ cả lượt): trang trông như tin thì bóc
                # bằng rule, không thì để lượt sau.
                if signals < 2 and not role_title(title or ""):
                    session.truncate("llm")
                    return None
            else:
                if result.value is None or not result.value.is_job_posting:
                    return None
                detail = result.value
                method = ExtractionMethod.LLM
                title = clean_text(detail.title) or title
        if not title or len(markdown) < 50:
            return None
        location = fields.get("location")
        extra: dict[str, object] = {"jd_signals": signals}
        extra.update((k, fields[k]) for k in _EXTRA_LABELS if k in fields)
        if description and description != markdown:
            extra["page_context"] = page_context(markdown, description)
        return RawJob(
            source=KIND,
            external_id=jsonld.external_id_for_url(page.url),
            url=page.url,
            title=title,
            company_name=detail.company_name if detail else None,
            description=description or markdown,
            description_format="markdown",
            locations=(detail.locations if detail and detail.locations else [])
            or ([location] if location else list(places)),
            work_mode=detail.work_mode if detail else None,
            employment_type=(detail.employment_type if detail else None)
            or fields.get("employment_type"),
            level=(detail.level if detail else None) or fields.get("level"),
            salary_raw=(detail.salary if detail else None) or fields.get("salary_raw"),
            experience_raw=(detail.experience if detail else None) or fields.get("experience_raw"),
            expires_raw=(detail.deadline if detail else None) or fields.get("expires_raw"),
            posted_raw=(detail.posted if detail else None) or fields.get("posted_raw") or published,
            apply_url=page.url,
            method=method,
            extra=extra,
        )

    # --- nguồn ---

    async def jobs(self, session: SourceSession) -> AsyncIterator[RawJob]:
        if session.polling and not session.plan.empty:
            async for job in self._poll(session):
                yield job
            return
        async for job in self._build(session):
            yield job

    async def _build(self, session: SourceSession) -> AsyncIterator[RawJob]:
        limits = session.limits
        plan = session.plan
        queue_start = normalize_url(self.url) or self.url
        queue = [queue_start]
        visited: set[str] = set()
        pending: dict[str, JobLink] = {}  # trang chi tiết cần mở, theo thứ tự gặp
        yielded: set[str] = set()  # external_id đã yield
        done_urls: set[str] = set()  # trang đã có đủ nội dung hoặc đã mở
        scans: list[ListingScan] = []
        # Link tin thấy trên trang danh sách / API / trang danh mục — lượt poll thấy lại được. Tin
        # chỉ tới được qua mục "vị trí khác" của trang tin khác là tin gián tiếp (`plan.indirect`).
        direct: set[str] = set()
        boards: set[str] = set()  # host của trang tin nhiều nhà tuyển dụng gặp ở lượt này

        def add_links(links: list[JobLink], *, listed: bool = True) -> None:
            for link in links:
                if listed:
                    direct.add(link.url)
                if link.url in done_urls:
                    continue
                current = pending.get(link.url)
                if current is None or (link.title and not current.title):
                    pending[link.url] = link

        def note(url: str, job: RawJob) -> RawJob:
            if url not in direct:
                plan.add_indirect(job.external_id)
            return job

        while queue and len(visited) < limits.max_listing_pages:
            url = queue.pop(0)
            if url in visited:
                continue
            if job_board_host(url):
                if url == queue_start:
                    raise SourceError(FetchOutcome.ERROR, f"{JOB_BOARD_PAGE}: {url}")
                continue
            visited.add(url)
            scan = await scan_with_render(session, url, required=not scans)
            if scan is None:
                continue
            visited.add(scan.page.url)
            if _job_board_listing(url, scan):
                if not scans and url == queue_start:
                    raise SourceError(FetchOutcome.ERROR, f"{JOB_BOARD_PAGE}: {url}")
                boards.add(host_of(scan.page.url))
                continue
            scans.append(scan)
            plan.add("listings", url)
            if scan.page.rendered:
                plan.add("rendered", url)
            for pager, _first in scan.apis:
                plan.add_api(api_spec(pager, url))
            for doc in scan.feeds:
                plan.add_api(feed_spec(doc, url))
            if scan.page.rendered and (scan.apis or scan.feeds) and not scan.uncovered:
                # Mọi tin trên trang đều có trong API bắt được: poll gọi API, không mở trang.
                plan.add("api_only", url)
            plan.add_names(scan.id_names)
            if scan.jobs:
                session.count(url, len(scan.jobs))
            for job in scan.jobs:
                if job.external_id not in yielded:
                    yielded.add(job.external_id)
                    done_urls.add(job.url)
                    if job.external_id in scan.feed_only:
                        plan.add_indirect(job.external_id)
                    yield job
            add_links([j for j in scan.job_links if j.url not in scan.feed_only])
            add_links([j for j in scan.job_links if j.url in scan.feed_only], listed=False)
            queue.extend(u for u in scan.next_pages if u not in visited)
            log.debug("careers.listing", url=scan.page.url, jobs=len(scan.jobs), links=len(pending))
        if any(u not in visited for u in queue):
            session.truncate("listing_pages")
        if boards:
            # Link tin dẫn vào trang của sàn (bài báo nhắc "tuyển dụng" kèm khối tin của chuyên
            # trang việc làm): tin của doanh nghiệp khác. Mọi link đều vậy: không phải nguồn.
            plan.boards = sorted(boards)
            linked = bool(pending)
            for link_url in [u for u in pending if host_of(u) in boards]:
                del pending[link_url]
            if linked and not pending and not yielded:
                raise SourceError(FetchOutcome.ERROR, f"{JOB_BOARD_PAGE}: {self.url}")

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
                if found or inline:
                    plan.llm_listing = True
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
        sitemaps_read = False
        while True:
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
                            yield note(url, known)
                        continue
                    done_urls.add(url)
                    batch.append(link)
                opened += len(batch)
                for link, (job, more, hub) in zip(
                    batch,
                    await gather_all(self._detail(session, link) for link in batch),
                    strict=True,
                ):
                    add_links(more, listed=hub)
                    if hub:
                        plan.add("hubs", link.url)
                        hubs += 1
                        if hubs <= limits.max_sub_listings:
                            opened -= 1
                        log.debug("careers.hub", url=link.url, links=len(more))
                        continue
                    if job is not None:
                        session.count(link.url, 1)
                    if job is not None and job.external_id not in yielded:
                        yielded.add(job.external_id)
                        yield note(link.url, job)
            if sitemaps_read:
                break
            sitemaps_read = True
            # Sitemap của site: đủ tin khi trang danh sách chỉ hiện một phần (phần còn lại nạp
            # bằng XHR / cuộn vô hạn). Chỉ lấy URL cùng khuôn với link tin trên trang danh sách /
            # danh mục (đọc sau khi mở trang danh mục để học khuôn từ cả các trang đó).
            for sitemap, links in await self._sitemaps(session, url_shapes(direct)):
                plan.add_sitemap(sitemap)
                session.count(sitemap.url, len(links))
                add_links(links)
        # Hết lượt mở trang: tin còn lại đã biết thì vẫn dùng lại được.
        for url in list(pending):
            known = reuse(url) if url not in done_urls else None
            if known is not None and known.external_id not in yielded:
                yielded.add(known.external_id)
                yield note(url, known)
        if any(url not in done_urls for url in pending):
            session.truncate("detail_pages")

    # --- poll theo bộ khung ---

    async def _poll_api(
        self,
        session: SourceSession,
        api: ApiSpec,
        seen: set[str],
        names: json_jobs.IdNames,
        budget: PageBudget,
    ) -> AsyncIterator[RawJob | JobLink]:
        """Gọi thẳng API danh sách tin bắt được ở lượt build (trang đầu rồi các trang sau)."""
        if not budget.take():
            return
        pager = api_pager(api)
        docs = await (self._api_page(session, pager, 0) if api.paged else self._feed(session, api))
        if docs is None:
            _plan_request_failed(
                session, api.url, kind="api", wrong_type="api_not_json", truncation="api_pages"
            )
            return
        page = Page(api.listing or self.url, "")
        read = 0
        for doc in docs:
            arrays = json_jobs.job_arrays(doc)
            if not arrays:
                continue
            read += len(arrays[0].items)
            pager = json_jobs.with_doc(pager, doc)
            extracted = json_jobs.extract(doc, page_url=page.url, links={}, names=names)
            for job in extracted.jobs:
                if job.external_id not in seen:
                    seen.add(job.external_id)
                    yield job
            for url, title in extracted.links:
                if url not in seen:
                    seen.add(url)
                    yield JobLink(url=url, title=title)
        if read == 0:
            session.mark_broken(f"api_no_jobs: {api.url}")
            return
        if not api.paged:
            return
        async for item in self._api_jobs(session, pager, read, page, {}, seen, names, budget):
            yield item

    async def _feed(self, session: SourceSession, api: ApiSpec) -> list[json_jobs.JsonDoc] | None:
        """Gọi lại đúng request JSON (không phân trang) bắt được ở lượt build."""
        try:
            result = await session.fetch(
                api.url,
                method=api.method,
                json=api.body if api.method != "GET" else None,
                headers={"Accept": "application/json, text/plain, */*"},
            )
        except SourceError:
            return None
        if not result.ok:
            return None
        try:
            data = result.json()
        except ValueError:
            return None
        return [json_jobs.JsonDoc(url=api.url, data=data, method=api.method, request_json=api.body)]

    async def _poll(self, session: SourceSession) -> AsyncIterator[RawJob]:
        """Đọc lại danh sách tin theo bộ khung; chỉ mở trang chi tiết của link chưa biết."""
        spec = session.plan
        limits = session.limits
        names = spec.name_map()
        yielded: set[str] = set()
        pending: dict[str, JobLink] = {}
        seen: set[str] = set()  # mã tin / URL đã thấy ở trang danh sách hoặc API
        ignored = set(spec.ignored)
        boards = set(spec.boards)
        listed = 0  # tin + link tin thấy trên danh sách / API

        def known_or(job: RawJob) -> RawJob:
            # Tin đã lưu: dùng bản đã lưu (nội dung đổi được cập nhật ở lượt build đêm).
            return session.reuse(key=(job.source, job.external_id)) or job

        # Số trang của bộ khung: API trước (rẻ, thường đủ tin), rồi trang danh sách / danh mục.
        budget = PageBudget.of(limits.poll_pages)

        # API danh sách tin (trang SPA): gọi thẳng, không mở trình duyệt.
        api_listings: set[str] = set()
        for api in spec.apis:
            if api.embedded:
                continue
            got = 0
            async for item in self._poll_api(session, api, seen, names, budget):
                got += 1
                if isinstance(item, JobLink):
                    pending.setdefault(item.url, item)
                elif item.external_id not in yielded:
                    yielded.add(item.external_id)
                    yield known_or(item)
            listed += got
            if got and api.listing in spec.api_only:
                api_listings.add(api.listing)

        # Sitemap: một request mà đủ link tin (trang danh sách chỉ hiện một phần).
        for sitemap in spec.sitemaps:
            if not budget.take():
                break
            text = await self._sitemap_text(session, sitemap.url)
            if text is None:
                _plan_request_failed(
                    session,
                    sitemap.url,
                    kind="sitemap",
                    wrong_type="sitemap_not_xml",
                    truncation="sitemap",
                )
                continue
            links = sitemap_links(text, sitemap.shapes)
            if not links:
                # Không còn URL cùng khuôn: site đổi cấu trúc URL (hoặc đã gỡ hết tin).
                session.mark_broken(f"sitemap_no_jobs: {sitemap.url}")
                continue
            session.count(sitemap.url, len(links))
            listed += len(links)
            for link in links:
                pending.setdefault(link.url, link)

        # Trang danh sách (bỏ trang SPA đã có API thay), trang sau, trang danh mục.
        rendered = set(spec.rendered)
        queue = [u for u in dict.fromkeys([*spec.listings, *spec.hubs]) if u not in api_listings]
        # Bản ngôn ngữ khác của cùng trang (/career, /ja/career, /ko/career): cùng các tin, chỉ đọc
        # bản đầu — mỗi bản có khi phải mở bằng trình duyệt, ăn hết giờ của lượt poll.
        variants: set[str] = set()
        unique: list[str] = []
        for url in queue:
            if (key := _locale_key(url)) not in variants:
                variants.add(key)
                unique.append(url)
        queue = unique
        first = spec.listings[0] if spec.listings and not api_listings else None
        visited: set[str] = set()
        scans: list[ListingScan] = []
        # Không có giới hạn bộ khung (0): vẫn chặn trang sau mới phát sinh như lượt build.
        cap = max(limits.max_listing_pages, len(queue))
        while queue and len(visited) < cap:
            url = queue.pop(0)
            if url in visited or job_board_host(url) or host_of(url) in boards:
                continue
            if not budget.take():
                queue.insert(0, url)
                break
            visited.add(url)
            # HTTP trước (rẻ); trang mà lượt build phải mở bằng trình duyệt chỉ render lại khi
            # HTML không có tin nào.
            scan = await scan_with_render(session, url, required=url == first, render=False)
            if url in rendered and (scan is None or scan.empty) and session.can_render:
                page = await fetch_page(
                    session, url, render=True, wait_until="networkidle", capture_json=True
                )
                scan = scan_listing(page) if page is not None else scan
            if scan is None:
                continue
            visited.add(scan.page.url)
            if _job_board_listing(url, scan):
                boards.add(host_of(scan.page.url))
                continue
            scans.append(scan)
            for job in scan.jobs:
                listed += 1
                if job.external_id not in yielded:
                    yielded.add(job.external_id)
                    seen.add(job.url)
                    yield known_or(job)
            for link in scan.job_links:
                listed += 1
                # Chữ trên link (gợi ý tiêu đề) thay cho link cùng URL từ sitemap.
                if link.url not in pending or (link.title and not pending[link.url].title):
                    pending[link.url] = link
            queue.extend(u for u in scan.next_pages if u not in visited)
            texts = {link.url: link.text for link in scan.links}
            scan_names = {**names, **scan.id_names}
            for pager, count in scan.apis:
                async for item in self._api_jobs(
                    session, pager, count, scan.page, texts, seen, scan_names, budget
                ):
                    listed += 1
                    if isinstance(item, JobLink):
                        pending.setdefault(item.url, item)
                    elif item.external_id not in yielded:
                        yielded.add(item.external_id)
                        yield known_or(item)
        if any(u not in visited for u in queue) and not budget.cut:
            session.truncate("listing_pages")

        if spec.llm_listing and not listed and scans and session.llm.enabled:
            for scan in _llm_listing_candidates(scans):
                found, inline = await self._llm_listing(session, scan)
                listed += len(found) + len(inline)
                for link in found:
                    pending.setdefault(link.url, link)
                for job in inline:
                    if job.external_id not in yielded:
                        yielded.add(job.external_id)
                        yield known_or(job)
                if found or inline:
                    break

        if not listed and spec.jobs > 0 and not session.truncated and session.broken is None:
            # Trang vẫn mở được mà không còn tin nào: cấu trúc đổi (hoặc đã gỡ hết tin) — build
            # lại để biết chắc.
            session.mark_broken("empty_listing")
            return
        if budget.cut:
            # Site nhiều trang hơn phần của nguồn trong bộ khung: tin mới ở các trang đầu vẫn thấy,
            # nhưng lượt poll không đọc trọn nên không đóng việc (lượt build đêm đọc đủ mới đóng).
            session.truncate("plan_pages")
        session.listed = True
        for link_url in [u for u in pending if host_of(u) in boards]:
            del pending[link_url]  # tin của doanh nghiệp khác trên sàn (`PlanSpec.boards`)

        # Trang chi tiết: tin đã biết dùng lại, link không phải tin bỏ qua, chỉ mở link mới.
        # Link tới trang danh sách / danh mục vừa đọc (danh mục cũng là "link tin" trên trang chủ)
        # không mở lại.
        done: set[str] = set(visited)
        opened = 0
        while pending and opened < limits.max_detail_pages:
            batch: list[JobLink] = []
            size = min(limits.detail_concurrency, limits.max_detail_pages - opened)
            while pending and len(batch) < size:
                url, link = next(iter(pending.items()))
                del pending[url]
                if url in done:
                    continue
                done.add(url)
                if (known := session.reuse(url=url)) is not None:
                    if known.external_id not in yielded:
                        yielded.add(known.external_id)
                        yield known
                    continue
                if url in ignored:
                    continue
                batch.append(link)
            opened += len(batch)
            results = await gather_all(self._detail(session, link) for link in batch)
            for link, (job, more, hub) in zip(batch, results, strict=True):
                for extra in more:
                    if extra.url not in done:
                        pending.setdefault(extra.url, extra)
                if hub or job is None:
                    continue
                session.count(link.url, 1)
                if job.external_id not in yielded:
                    yielded.add(job.external_id)
                    yield job
        # Hết lượt mở trang: tin còn lại đã biết vẫn dùng lại được.
        for url in list(pending):
            known = session.reuse(url=url)
            if known is not None:
                del pending[url]
                if known.external_id not in yielded:
                    yielded.add(known.external_id)
                    yield known
        if any(u not in ignored and u not in done for u in pending):
            session.truncate("detail_pages")
