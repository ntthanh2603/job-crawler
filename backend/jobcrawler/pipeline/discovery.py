"""Tìm nguồn tin trên website doanh nghiệp: trang careers và bảng tin ATS (cache 7 ngày).

Thứ tự: link trên trang chủ (chữ/đường dẫn có từ khoá tuyển dụng) → sitemap.xml → subdomain
careers./tuyendung./jobs. → các đường dẫn phổ biến. Trang chủ thử cùng lúc domain trần và `www.`.
Nhiều link ứng viên thì LLM chọn trong danh sách (chỉ trả số thứ tự). Mỗi trang ứng viên được
mở để xác nhận (có tin, JSON-LD, JSON của trang SPA, hoặc nhúng ATS); chưa xác nhận thì đi tiếp
tối đa `MAX_HOPS` bước nhảy tính từ trang chủ. Không có vòng lặp agent: mọi bước đều có giới hạn
cố định.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from jobcrawler.common.logging import get_logger
from jobcrawler.common.pages import Link, extract_links, node_text, parse
from jobcrawler.common.text import strip_accents
from jobcrawler.common.urls import (
    host_of,
    normalize_url,
    parse_domain_input,
    path_segments,
    registrable_domain,
    same_site,
    strip_www,
)
from jobcrawler.prompts import careers_pick
from jobcrawler.services.net.http import FetchResult
from jobcrawler.services.net.kv import KeyValueStore
from jobcrawler.sources import ats
from jobcrawler.sources.ats.ref import AtsRef
from jobcrawler.sources.base import Source, SourceError, SourceSession
from jobcrawler.sources.boards.base import is_social_host
from jobcrawler.sources.careers import (
    JOB_BOARDS,
    STOP_SEGMENTS,
    CareersSource,
    Page,
    fetch_page,
    job_board_host,
    listing_links,
    multi_employer_page,
    scan_with_render,
)

log = get_logger(__name__)

CACHE_VERSION = 1
TIMEOUT = "timeout"
MAX_HOPS = 3  # tính từ trang chủ
MAX_EVALUATE = 3  # số trang ứng viên mở để xác nhận ở mỗi bước
MAX_CAREERS_PAGES = 2
MAX_SITEMAPS = 3
MAX_LLM_CANDIDATES = 20
# URL đoán (đường dẫn phổ biến, subdomain): đa số không tồn tại, không thử lại, chờ ngắn.
GUESS_TIMEOUT = 8.0
# LLM chọn link careers quá lâu (LLM quá tải) thì chọn theo điểm: không để bước này ăn hết giờ.
CHOOSE_TIMEOUT = 10.0

COMMON_PATHS = (
    "/careers",
    "/career",
    "/jobs",
    "/tuyen-dung",
    "/co-hoi-nghe-nghiep",
    "/en/careers",
    "/vi/tuyen-dung",
    "/recruitment",
    "/tuyen-dung.html",
    "/careers.html",
)
SUBDOMAINS = ("careers", "tuyendung", "jobs", "career", "vieclam", "recruitment", "job")

_CAREERS_TEXT = re.compile(
    r"\b(tuyen dung|co hoi nghe nghiep|co hoi viec lam|co hoi lam viec|viec lam|nghe nghiep|"
    r"gia nhap|careers?|jobs?|join (us|our team)|work (with|for) us|we.?re hiring|hiring|"
    r"recruitment|vacancies|open positions)\b"
)
_CAREERS_SEGMENTS = frozenset(
    {
        "career",
        "careers",
        "jobs",
        "job",
        "tuyen-dung",
        "tuyendung",
        "co-hoi-nghe-nghiep",
        "co-hoi-viec-lam",
        "viec-lam",
        "recruitment",
        "join-us",
        "joinus",
        "nghe-nghiep",
        "lam-viec-tai",
        "hiring",
        "vacancies",
    }
)
_CAREERS_HOSTS = (
    "careers.",
    "career.",
    "jobs.",
    "job.",
    "tuyendung.",
    "vieclam.",
    "recruit.",
    "talent.",
    "hr.",
)
# Chữ trong tên host của trang tuyển dụng đặt ở domain riêng (momo.careers, job.matbao.com,
# tuyendungeximbank.com, bvbank.talent.vn).
_CAREERS_HOST_WORDS = (
    "career",
    "tuyendung",
    "tuyen-dung",
    "job",
    "recruit",
    "vieclam",
    "talent",
    "nhansu",
)
# Mục tuyển dụng nằm trong mục tin tức / blog ("/tin-tuc/tin-tuyen-dung-2", "/category/tuyen-dung").
_CAREERS_POSTS = frozenset({"tin-tuyen-dung", "thong-tin-tuyen-dung", "thong-bao-tuyen-dung"})
# Chữ trên link chỉ là tên mục tuyển dụng: link ra domain khác vẫn đáng mở (trang tuyển dụng của
# tập đoàn mẹ, hệ thống tuyển dụng thuê ngoài) — trừ mạng xã hội và trang tuyển dụng chung.
_CAREERS_LABEL = re.compile(
    r"^(tuyen dung|tuyen dung nhan su|thong tin tuyen dung|co hoi nghe nghiep|co hoi viec lam|"
    r"viec lam|careers?|jobs|join us|recruitment|we.?re hiring)$"
)
# Đoạn cuối của URL tin chi tiết: id số, hoặc slug kết thúc bằng id (`ke-toan-12`, `sales_29675`).
_DETAIL_SLUG = re.compile(r"^\d+$|[a-z].*[-_]\d+$")
# Bị chuyển tới trang lỗi có đường dẫn riêng (soft 404): /vi-VN/Error-404, /not-found.
_ERROR_PAGE = re.compile(r"/(error|404|not-?found|page-not-found|khong-tim-thay)\b", re.I)
# Tiêu đề của trang lỗi trả mã 200 (đã bỏ dấu, viết thường).
_ERROR_TITLE = re.compile(
    r"\b(404|page not found|not found|nothing found|khong tim thay( trang)?|"
    r"trang khong ton tai|thong bao loi|da xay ra loi)\b"
)
_LOC = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?\s*([^<\]\s]+)", re.IGNORECASE)
_ROBOTS_SITEMAP = re.compile(r"^\s*sitemap:\s*(\S+)", re.IGNORECASE | re.MULTILINE)


class DiscoveryResult(BaseModel):
    domain: str
    homepage: str | None = None
    careers_pages: list[str] = Field(default_factory=list)
    ats: list[AtsRef] = Field(default_factory=list)
    unsupported_ats: list[AtsRef] = Field(default_factory=list)
    # homepage | sitemap | common_path | subdomain | input | input_ats | unconfirmed
    method: str | None = None
    steps: list[str] = Field(default_factory=list)  # dấu vết để debug
    error: str | None = None
    discovered_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    from_cache: bool = False

    @property
    def found(self) -> bool:
        return bool(self.careers_pages or self.ats)


@dataclass(frozen=True, slots=True)
class Candidate:
    url: str
    text: str
    score: int


def _plain(text: str) -> str:
    return " ".join(strip_accents(text).lower().split())


def _segments(url: str) -> list[str]:
    return [strip_accents(s).lower().removesuffix(".html") for s in path_segments(url)]


def brand_of(site: str) -> str | None:
    """Nhãn thương hiệu của domain: `www.momo.vn` → `momo`, `fpt.com.vn` → `fpt`."""
    label = registrable_domain(host_of(site) if "/" in site else site).split(".")[0]
    return label if len(label) >= 3 else None


def brand_careers_site(url: str, site: str) -> bool:
    """Trang tuyển dụng đặt ở domain riêng mang tên thương hiệu: momo.careers,
    haravan-careers.myharavan.com, vngcareers.com, job.matbao.com (site matbao.net),
    tuyendung.ivy.com.vn (site ivymoda.com), careersdecathlonvn.com..."""
    brand = brand_of(site)
    host = host_of(url)
    if brand is None or same_site(url, site):
        return False
    tokens = [t for t in re.split(r"[.-]", host) if len(t) >= 3 and t not in ("www", "com")]
    has_brand = any(brand in t or brand.startswith(t) for t in tokens)
    return has_brand and any(k in host for k in _CAREERS_HOST_WORDS)


def careers_segment(segment: str) -> bool:
    """Đoạn đường dẫn là mục tuyển dụng (`tuyen-dung`, `careers`, `tin-tuyen-dung-2`)."""
    base = re.sub(r"-\d{1,2}$", "", segment)  # bản trùng tên của WordPress, không phải năm
    return base in _CAREERS_SEGMENTS or base in _CAREERS_POSTS


def _offsite_careers(url: str, text: str) -> bool:
    """Link ra domain khác mà chữ chỉ là tên mục tuyển dụng ("Tuyển dụng" → tuyendung.frt.vn)."""
    domain = registrable_domain(host_of(url))
    return (
        bool(_CAREERS_LABEL.match(_plain(text)))
        and domain not in JOB_BOARDS
        and not is_social_host(domain)
    )


def careers_score(url: str, text: str, site: str) -> int:
    """Mức độ link trông như trang tuyển dụng của chính site (0 = không phải)."""
    brand_site = brand_careers_site(url, site)
    if not same_site(url, site) and not brand_site:
        # Domain khác không mang tên thương hiệu: chỉ khi chữ trên link đúng là "Tuyển dụng"
        # (evaluate vẫn phải thấy tin mới nhận).
        return 2 if _offsite_careers(url, text) else 0
    segs = _segments(url)
    stops = [i for i, s in enumerate(segs) if s in STOP_SEGMENTS]
    # Mục tuyển dụng trong mục tin tức / blog vẫn là trang tuyển dụng ("/tin-tuc/tuyen-dung",
    # "/category/tuyen-dung", "/about-us/careers"); bài viết trong mục tin tức thì không.
    if stops and not any(careers_segment(s) for s in segs[stops[-1] + 1 :]):
        return 0
    score = 0
    if _CAREERS_TEXT.search(_plain(text)):
        score += 3
    if any(careers_segment(s) for s in segs):
        score += 2
    host = host_of(url)
    if (host.startswith(_CAREERS_HOSTS) and host != host_of(site)) or brand_site:
        score += 2
    # Trang careers thường nông; link sâu (bài viết) kém tin hơn.
    if score and len(segs) > 3:
        score -= 1
    return score


def detail_parent(url: str) -> str | None:
    """URL tin chi tiết nằm dưới đoạn careers (`/tuyen-dung/ke-toan-12`, `/job/hn-sales-29675`):
    trang danh sách cha (`/tuyen-dung`, `/job`); không phải thì None."""
    segs = path_segments(url)
    if len(segs) < 2 or not _DETAIL_SLUG.search(
        strip_accents(segs[-1]).lower().removesuffix(".html")
    ):
        return None
    for i, seg in enumerate(segs[:-1]):
        if strip_accents(seg).lower().removesuffix(".html") in _CAREERS_SEGMENTS:
            return normalize_url("/" + "/".join(segs[: i + 1]), url)
    return None


def careers_candidates(links: list[Link], site: str) -> list[Candidate]:
    best: dict[str, Candidate] = {}
    for link in links:
        url, text = link.url, link.text
        # Link tới một tin (trang careers liệt kê tin, trang chủ có "tin nổi bật"): thử trang danh
        # sách cha. Không có LLM chọn, trang tin (có JobPosting) sẽ được nhận làm nguồn và doanh
        # nghiệp chỉ còn đúng một tin.
        if (parent := detail_parent(url)) is not None:
            url, text = parent, ""
        score = careers_score(url, text, site)
        if score < 2:
            continue
        current = best.get(url)
        if current is None or score > current.score:
            best[url] = Candidate(url, text, score)
    return sorted(best.values(), key=lambda c: -c.score)


def _fingerprint(html: str) -> str:
    return hashlib.sha1(" ".join(html.split()).encode()).hexdigest()


def sitemap_locations(xml: str) -> list[str]:
    return [m.group(1).strip() for m in _LOC.finditer(xml)]


def cache_id(website: str) -> str:
    """Khoá cache: domain, hoặc cả đường dẫn khi input là URL có path (hai bảng tin khác nhau
    trên cùng host ATS, hay trang careers chỉ định sẵn, không được dùng chung kết quả)."""
    raw = website.strip()
    url = normalize_url(raw if "://" in raw else f"https://{raw}")
    domain = parse_domain_input(website)
    if url is None or not path_segments(url):
        return domain
    rest = url.split("://", 1)[1]
    return strip_www(rest.rstrip("/"))


def _cache_key(key: str) -> str:
    return f"discovery:v{CACHE_VERSION}:{key}"


async def load_cached(kv: KeyValueStore, key: str) -> DiscoveryResult | None:
    blob = await kv.get(_cache_key(key))
    if blob is None:
        return None
    try:
        result = DiscoveryResult.model_validate_json(blob)
    except ValueError:
        return None
    result.from_cache = True
    return result


async def save_cached(
    kv: KeyValueStore, key: str, result: DiscoveryResult, *, ttl: float, negative_ttl: float
) -> None:
    effective = ttl if result.found else negative_ttl
    if effective > 0:
        payload = result.model_dump_json(exclude={"from_cache"})
        await kv.set(_cache_key(key), payload, ttl=effective)


class _Discovery:
    def __init__(self, session: SourceSession, domain: str, origin: str | None = None) -> None:
        self.s = session
        self.domain = domain
        # Gốc đúng như người dùng nhập khi có scheme / cổng riêng (`http://site.vn:8080/`).
        self.origin = origin
        self.result = DiscoveryResult(domain=domain)
        self.site = f"https://{domain}/"
        self.evaluated: set[str] = set()
        self.unconfirmed: list[str] = []
        self.blocked: str | None = None  # lý do trang chủ bị chặn

    def step(self, text: str) -> None:
        self.result.steps.append(text)

    def add_ats(self, refs: list[AtsRef]) -> int:
        added = 0
        for ref in refs:
            bucket = self.result.ats if ats.supported(ref) else self.result.unsupported_ats
            if all(r.name != ref.name for r in bucket):
                bucket.append(ref)
                added += 1
        return added

    def has_page(self, url: str) -> bool:
        key = url.rstrip("/")
        return any(p.rstrip("/") == key for p in self.result.careers_pages)

    async def homepage(self) -> Page | None:
        """Trang chủ: thử nhanh (không thử lại) cùng lúc domain trần, `www.`, cả https lẫn http
        (website doanh nghiệp nhỏ hay chỉ có http), và gốc đúng như người dùng nhập, rồi đọc bản
        mở được trước — domain trần không phản hồi (hoaphat.com.vn) không ăn hết giờ tìm nguồn."""
        hosts = [self.domain]
        if not self.domain.startswith("www."):
            hosts.append(f"www.{self.domain}")
        urls = [f"{scheme}://{host}/" for scheme in ("https", "http") for host in hosts]
        if self.origin is not None and self.origin not in urls:
            urls.insert(0, self.origin)
        ordered = await self._first_ok(urls)
        for url in ordered:
            try:
                page = await fetch_page(self.s, url)
            except SourceError as exc:  # WAF chặn: vẫn thử bản khác, rồi subdomain
                self.step(f"{url} → {exc}")
                self.blocked = f"{exc.outcome}: {exc}"
                continue
            if page is not None:
                self.result.homepage = page.url
                # Trang chủ chuyển sang domain khác (vd. brand.com → brand.com.vn): dùng domain mới.
                self.site = page.url
                return page
            self.step(f"{url} → không mở được")
        return None

    async def _first_ok(self, urls: list[str]) -> list[str]:
        """`urls` xếp lại: bản mở được nhanh nhất lên trước (không chờ bản còn treo)."""
        tasks = [
            asyncio.ensure_future(self.s.fetch(url, timeout=GUESS_TIMEOUT, retries=0))
            for url in urls
        ]
        try:
            for done in asyncio.as_completed(tasks):
                try:
                    result = await done
                except SourceError:
                    continue  # bị chặn: lần đọc trang chủ sau sẽ báo lỗi
                if result.ok and result.url in urls:
                    return [result.url, *(u for u in urls if u != result.url)]
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return urls

    async def choose(self, candidates: list[Candidate]) -> list[Candidate]:
        """LLM chọn trong danh sách khi có nhiều ứng viên; không có LLM thì theo điểm."""
        candidates = candidates[:MAX_LLM_CANDIDATES]
        # Ít ứng viên thì mở hết, không cần LLM chọn.
        if len(candidates) <= MAX_EVALUATE or not self.s.llm.enabled:
            return candidates[:MAX_EVALUATE]
        try:
            async with asyncio.timeout(CHOOSE_TIMEOUT):
                pick = await self.s.llm.structured(
                    careers_pick.PROMPT,
                    careers_pick.CareersPick,
                    site=self.site,
                    links="\n".join(
                        f"{i}. {c.text or '-'} — {c.url}" for i, c in enumerate(candidates)
                    ),
                )
        except TimeoutError:
            self.step("LLM chọn quá lâu: chọn theo điểm")
            return candidates[:MAX_EVALUATE]
        if pick.value is None:
            return candidates[:MAX_EVALUATE]
        chosen = [
            candidates[i] for i in dict.fromkeys(pick.value.choices) if 0 <= i < len(candidates)
        ]
        self.step(f"llm chọn {[c.url for c in chosen]}: {pick.value.reason}")
        return chosen[:MAX_EVALUATE]

    async def evaluate(self, candidate: Candidate, hop: int, *, render: bool = True) -> bool:
        """Mở trang ứng viên; xác nhận nếu có tin/JSON-LD/ATS, không thì đi tiếp (≤ MAX_HOPS).

        `render=False`: không mở trình duyệt (đường dẫn đoán mò, thường là soft 404 của SPA).
        """
        url = normalize_url(candidate.url) or candidate.url
        if url in self.evaluated or len(self.result.careers_pages) >= MAX_CAREERS_PAGES:
            return False
        self.evaluated.add(url)
        if job_board_host(url):
            self.step(f"{url} → trang của sàn tuyển dụng")
            return False
        try:
            scan = await scan_with_render(self.s, url, required=False, render=render)
        except SourceError as exc:  # trang ứng viên bị chặn: thử ứng viên khác
            self.step(f"{url} → {exc}")
            return False
        if scan is None:
            self.step(f"{url} → không mở được")
            return False
        final_url, html, links = scan.page.url, scan.page.html, scan.links
        if final_url != url and final_url in self.evaluated:
            return False  # nhiều đường dẫn cùng chuyển về một trang (vd. trang lỗi)
        self.evaluated.add(final_url)
        if _ERROR_PAGE.search(final_url):
            self.step(f"{url} → trang lỗi {final_url}")
            return False
        if job_board_host(final_url) or multi_employer_page(links):
            self.step(f"{url} → trang tin của nhiều nhà tuyển dụng (sàn)")
            return False
        tree = parse(html)
        title = _plain(node_text(tree.css_first("h1")) or node_text(tree.css_first("title")))
        page_title = _plain(node_text(tree.css_first("title")))
        if _ERROR_TITLE.search(f"{page_title} {title}"):
            # Trang lỗi trả mã 200 (soft 404) vẫn có menu / link tin của site.
            self.step(f"{url} → trang lỗi ({page_title or title})")
            return False
        home = self.result.homepage
        redirected = final_url != url and not path_segments(final_url)
        if redirected and home and host_of(final_url) == host_of(home):
            self.step(f"{url} → chuyển về trang chủ")
            return False
        if self.add_ats(ats.detect(html, final_url)):
            self.step(f"{final_url}: nhúng ATS")
            return True
        if not scan.empty:
            if not self.has_page(final_url):
                self.result.careers_pages.append(final_url)
            how = " (trình duyệt)" if scan.page.rendered else ""
            self.step(
                f"{final_url}{how}: {len(scan.job_links)} link tin, {len(scan.jobs)} JobPosting"
            )
            return True
        careers_like = _CAREERS_TEXT.search(title) or any(
            s in _CAREERS_SEGMENTS for s in _segments(final_url)
        )
        if careers_like:
            self.unconfirmed.append(final_url)
        self.step(f"{final_url}: không thấy tin" + (" (trang tuyển dụng)" if careers_like else ""))
        if hop >= MAX_HOPS:
            return False
        # Bước tiếp: link "xem tất cả vị trí", link careers sâu hơn, hoặc link ATS trên trang.
        nexts = [Candidate(u, "", 3) for u in listing_links(links, final_url)]
        nexts += [c for c in careers_candidates(links, final_url) if c.url != final_url]
        nexts = [c for c in nexts if (normalize_url(c.url) or c.url) not in self.evaluated]
        for nxt in (await self.choose(nexts)) if nexts else []:
            if await self.evaluate(nxt, hop + 1):
                return True
        return False

    def root(self) -> str:
        """Gốc site theo trang chủ đã mở (giữ `http://` của site chỉ có http)."""
        parts = urlsplit(self.site)
        return f"{parts.scheme or 'https'}://{parts.netloc or host_of(self.site)}"

    async def _guess(self, url: str, **kwargs: Any) -> FetchResult | None:
        """Mở URL đoán. Bị chặn (WAF chặn `sitemap.xml`, một subdomain) thì bỏ qua URL đó, không
        dừng cả bước tìm nguồn."""
        try:
            return await self.s.fetch(url, **kwargs)
        except SourceError as exc:
            self.step(f"{url} → {exc}")
            return None

    async def robots_sitemaps(self) -> list[str]:
        """Sitemap khai trong robots.txt (`sitemap.ashx`, `sitemap-index.xml`...)."""
        result = await self._guess(f"{self.root()}/robots.txt", check_robots=False)
        if result is None or not result.ok:
            return []
        return [m.group(1) for m in _ROBOTS_SITEMAP.finditer(result.text)][:MAX_SITEMAPS]

    async def sitemap(self) -> list[Candidate]:
        root = self.root()
        found: dict[str, Candidate] = {}
        queue = list(
            dict.fromkeys(
                [*await self.robots_sitemaps(), f"{root}/sitemap.xml", f"{root}/sitemap_index.xml"]
            )
        )
        fetched = 0
        while queue and fetched < MAX_SITEMAPS:
            url = queue.pop(0)
            fetched += 1
            result = await self._guess(url)
            if result is None or not result.ok:
                continue
            locs = sitemap_locations(result.text)
            if "<sitemapindex" in result.text[:2000].lower():
                # Ưu tiên sitemap của trang tĩnh / tuyển dụng.
                children = sorted(
                    locs, key=lambda u: not re.search(r"page|career|job|tuyen|recruit", u, re.I)
                )
                queue = [*children[: MAX_SITEMAPS - fetched], *queue]
                continue
            for loc in locs:
                if (score := careers_score(loc, "", self.site)) < 2:
                    continue
                segs = path_segments(loc)
                # URL tin chi tiết (/tuyen-dung/ke-toan-123) → trang danh sách cha.
                for i, seg in enumerate(segs):
                    if careers_segment(strip_accents(seg).lower().removesuffix(".html")):
                        parent = normalize_url("/" + "/".join(segs[: i + 1]), loc) or loc
                        found.setdefault(parent, Candidate(parent, "", score + 1))
                        break
            if found:
                break
        return sorted(found.values(), key=lambda c: (-c.score, len(c.url)))

    async def probe_common_paths(self) -> bool:
        root = self.root()
        home = await self._guess(self.site)  # đã có trong HTTP cache
        home_print = _fingerprint(home.text) if home is not None and home.ok else None
        for path in COMMON_PATHS:
            candidate = Candidate(f"{root}{path}", "", 2)
            result = await self._guess(candidate.url, timeout=GUESS_TIMEOUT, retries=0)
            # Đường dẫn không tồn tại thường bị chuyển về trang chủ, hoặc (SPA) trả nguyên khung
            # HTML của trang chủ với mã 200: soft 404, bỏ qua.
            if result is None or not result.ok or not path_segments(result.final_url):
                continue
            if home_print is not None and _fingerprint(result.text) == home_print:
                self.step(f"{candidate.url}: giống trang chủ (soft 404)")
                continue
            if await self.evaluate(candidate, hop=1, render=False):
                return True
        return False

    async def probe_subdomains(self) -> bool:
        base = registrable_domain(host_of(self.site))
        scheme = urlsplit(self.site).scheme or "https"
        # DNS wildcard: mọi subdomain đều trả cùng một trang (trang chủ, trang đăng nhập email,
        # trang mặc định của máy chủ) — thử một tên ngẫu nhiên trước để nhận ra.
        wildcard = await self._guess(
            f"{scheme}://jc{secrets.token_hex(4)}.{base}/", timeout=GUESS_TIMEOUT, retries=0
        )
        wild_print = _fingerprint(wildcard.text) if wildcard is not None and wildcard.ok else None
        for sub in SUBDOMAINS:
            url = f"{scheme}://{sub}.{base}/"
            # Subdomain không tồn tại (lỗi DNS/kết nối): bỏ qua ngay, không thử lại.
            probe = await self._guess(url, timeout=GUESS_TIMEOUT, retries=0)
            if probe is None or probe.status is None:
                continue
            if wild_print is not None and _fingerprint(probe.text) == wild_print:
                continue
            final = host_of(probe.final_url)
            redirected_home = final != host_of(url) and final == host_of(self.site)
            if redirected_home and not path_segments(probe.final_url):
                continue  # chuyển về trang chủ
            if await self.evaluate(Candidate(url, "", 2), hop=1):
                return True
        return False

    async def run(self, input_url: str | None) -> DiscoveryResult:
        r = self.result
        if input_url and (ref := ats.match_url(input_url)) is not None:
            self.add_ats([ref])
            r.method = "input_ats"
            return r
        home = await self.homepage()
        if home is None:
            # Trang chủ bị WAF chặn / không phản hồi nhưng trang tuyển dụng ở subdomain riêng
            # (tuyendung.<domain>) thường vẫn mở được.
            if await self.probe_subdomains():
                r.method = "subdomain"
                return r
            r.error = self.blocked or "không mở được trang chủ"
            return r
        links = extract_links(parse(home.html), home.url)
        candidates = careers_candidates(links, home.url)
        if not candidates and not home.rendered:
            # Menu dựng bằng JavaScript: mở lại trang chủ bằng trình duyệt.
            rendered = await fetch_page(self.s, home.url, render=True)
            if rendered is not None:
                home = rendered
                links = extract_links(parse(home.html), home.url)
                candidates = careers_candidates(links, home.url)
                self.step(f"trang chủ (trình duyệt): {len(links)} link")
        if self.add_ats(ats.detect(home.html, home.url)):
            self.step("trang chủ nhắc tới ATS")
            r.method = "homepage"

        if input_url and path_segments(input_url):
            candidates.insert(0, Candidate(input_url, "", 9))
            r.method = r.method or "input"
        self.step(f"trang chủ: {len(candidates)} link ứng viên")
        for candidate in await self.choose(candidates):
            if await self.evaluate(candidate, hop=1):
                r.method = r.method or "homepage"
        if r.found:
            return r

        if sitemap := await self.sitemap():
            self.step(f"sitemap: {len(sitemap)} ứng viên")
            for candidate in await self.choose(sitemap):
                if await self.evaluate(candidate, hop=1):
                    r.method = "sitemap"
                    return r
        # Subdomain trước: tuyendung.<domain> là chỗ hay gặp nhất mà chỉ mất vài request, còn
        # dò 10 đường dẫn trên site lớn chậm (Viettel hết giờ trước khi tới subdomain).
        if await self.probe_subdomains():
            r.method = "subdomain"
            return r
        if await self.probe_common_paths():
            r.method = "common_path"
            return r
        # Không xác nhận được trang nào: giữ trang có dấu hiệu careers rõ nhất để thử bằng LLM.
        if self.unconfirmed and not r.ats:
            r.careers_pages.append(self.unconfirmed[0])
            r.method = r.method or "unconfirmed"
        return r


async def discover(
    session: SourceSession,
    kv: KeyValueStore,
    website: str,
    *,
    refresh: bool = False,
    cache_ttl: float = 7 * 86_400,
    negative_ttl: float = 86_400,
    timeout: float | None = None,
) -> DiscoveryResult:
    """Tìm trang careers và ATS của một website (domain hoặc URL).

    Hết `timeout` (giây) thì dừng và trả những gì đã tìm được, `error="timeout"`; kết quả hết
    giờ không được cache để lượt sau tìm lại đầy đủ.
    """
    domain = parse_domain_input(website)
    key = cache_id(website)
    if not refresh and (cached := await load_cached(kv, key)) is not None:
        log.info("discovery.done", domain=domain, cached=True, **_summary(cached))
        return cached
    raw = website.strip()
    input_url = normalize_url(raw if "://" in raw else f"https://{raw}")
    origin = None
    if input_url is not None and "://" in raw:
        parts = urlsplit(input_url)
        origin = f"{parts.scheme}://{parts.netloc}/"
    run = _Discovery(session, domain, origin)
    try:
        async with asyncio.timeout(timeout):
            result = await run.run(input_url)
    except SourceError as exc:
        result = run.result
        result.error = f"{exc.outcome}: {exc}"
    except TimeoutError:
        result = run.result
        result.error = TIMEOUT
        # Hết giờ trước khi xác nhận được trang nào: thử trang có dấu hiệu careers rõ nhất.
        if not result.found and run.unconfirmed:
            result.careers_pages.append(run.unconfirmed[0])
            result.method = result.method or "unconfirmed"
    # Input là URL bảng ATS thì không cần cache (không gọi mạng).
    cacheable = result.error is None or (result.found and result.error != TIMEOUT)
    if cacheable and result.method != "input_ats":
        await save_cached(kv, key, result, ttl=cache_ttl, negative_ttl=negative_ttl)
    log.info("discovery.done", domain=domain, cached=False, **_summary(result))
    return result


def _summary(result: DiscoveryResult) -> dict[str, object]:
    return {
        "careers_pages": result.careers_pages,
        "ats": [r.name for r in result.ats],
        "unsupported_ats": [r.name for r in result.unsupported_ats],
        "method": result.method,
        "error": result.error,
    }


def sources_for(result: DiscoveryResult) -> list[Source]:
    """Nguồn cần thu thập từ kết quả tìm kiếm (ATS trước, rồi trang careers)."""
    sources: list[Source] = [s for ref in result.ats if (s := ats.source_for(ref)) is not None]
    seen = {s.name for s in sources}
    for url in result.careers_pages:
        if job_board_host(url):  # kết quả cache từ trước khi có luật này
            continue
        source = CareersSource(url)
        if source.name not in seen:
            seen.add(source.name)
            sources.append(source)
    return sources
