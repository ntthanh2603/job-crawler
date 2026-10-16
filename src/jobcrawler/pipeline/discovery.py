"""Tìm nguồn tin trên website doanh nghiệp: trang careers và bảng tin ATS (cache 7 ngày).

Thứ tự: link trên trang chủ (chữ/đường dẫn có từ khoá tuyển dụng) → sitemap.xml → các đường
dẫn phổ biến → subdomain careers./tuyendung./jobs. Nhiều link ứng viên thì LLM chọn trong danh
sách (chỉ trả số thứ tự). Mỗi trang ứng viên được mở để xác nhận (có tin, JSON-LD, hoặc nhúng
ATS); chưa xác nhận thì đi tiếp tối đa `MAX_HOPS` bước nhảy tính từ trang chủ. Không có vòng
lặp agent: mọi bước đều có giới hạn cố định.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from jobcrawler.logging import get_logger
from jobcrawler.net.kv import KeyValueStore
from jobcrawler.net.urls import (
    host_of,
    normalize_url,
    parse_domain_input,
    path_segments,
    registrable_domain,
    same_site,
    strip_www,
)
from jobcrawler.pages import Link, extract_links, node_text, parse
from jobcrawler.prompts import careers_pick
from jobcrawler.sources import ats
from jobcrawler.sources.ats.ref import AtsRef
from jobcrawler.sources.base import Source, SourceError, SourceSession
from jobcrawler.sources.careers import (
    STOP_SEGMENTS,
    CareersSource,
    Page,
    fetch_page,
    listing_links,
    scan_with_render,
)
from jobcrawler.text import strip_accents

log = get_logger(__name__)

CACHE_VERSION = 1
TIMEOUT = "timeout"
MAX_HOPS = 3  # tính từ trang chủ
MAX_EVALUATE = 3  # số trang ứng viên mở để xác nhận ở mỗi bước
MAX_CAREERS_PAGES = 2
MAX_SITEMAPS = 3
MAX_LLM_CANDIDATES = 20

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
SUBDOMAINS = ("careers", "tuyendung", "jobs")

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
_CAREERS_HOSTS = ("careers.", "career.", "jobs.", "tuyendung.", "recruit.", "hr.")
# Bị chuyển tới trang lỗi có đường dẫn riêng (soft 404): /vi-VN/Error-404, /not-found.
_ERROR_PAGE = re.compile(r"/(error|404|not-?found|page-not-found|khong-tim-thay)\b", re.I)
_LOC = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?\s*([^<\]\s]+)", re.IGNORECASE)


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
    haravan-careers.myharavan.com, vngcareers.com..."""
    brand = brand_of(site)
    host = host_of(url)
    if brand is None or same_site(url, site):
        return False
    tokens = re.split(r"[.-]", host)
    has_brand = brand in tokens or any(t.startswith(brand) and len(t) > len(brand) for t in tokens)
    careers_word = any(k in host for k in ("career", "tuyendung", "tuyen-dung", "jobs", "recruit"))
    return has_brand and careers_word


def careers_score(url: str, text: str, site: str) -> int:
    """Mức độ link trông như trang tuyển dụng của chính site (0 = không phải)."""
    brand_site = brand_careers_site(url, site)
    if not same_site(url, site) and not brand_site:
        return 0
    segs = _segments(url)
    if any(s in STOP_SEGMENTS for s in segs):
        return 0
    score = 0
    if _CAREERS_TEXT.search(_plain(text)):
        score += 3
    if any(s in _CAREERS_SEGMENTS for s in segs):
        score += 2
    host = host_of(url)
    if (host.startswith(_CAREERS_HOSTS) and host != host_of(site)) or brand_site:
        score += 2
    # Trang careers thường nông; link sâu (bài viết) kém tin hơn.
    if score and len(segs) > 3:
        score -= 1
    return score


def careers_candidates(links: list[Link], site: str) -> list[Candidate]:
    best: dict[str, Candidate] = {}
    for link in links:
        score = careers_score(link.url, link.text, site)
        if score < 2:
            continue
        current = best.get(link.url)
        if current is None or score > current.score:
            best[link.url] = Candidate(link.url, link.text, score)
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
    def __init__(self, session: SourceSession, domain: str) -> None:
        self.s = session
        self.domain = domain
        self.result = DiscoveryResult(domain=domain)
        self.site = f"https://{domain}/"
        self.evaluated: set[str] = set()
        self.unconfirmed: list[str] = []

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
        for url in (f"https://{self.domain}/", f"https://www.{self.domain}/"):
            page = await fetch_page(self.s, url)
            if page is not None:
                self.result.homepage = page.url
                # Trang chủ chuyển sang domain khác (vd. brand.com → brand.com.vn): dùng domain mới.
                self.site = page.url
                return page
            self.step(f"{url} → không mở được")
        return None

    async def choose(self, candidates: list[Candidate]) -> list[Candidate]:
        """LLM chọn trong danh sách khi có nhiều ứng viên; không có LLM thì theo điểm."""
        candidates = candidates[:MAX_LLM_CANDIDATES]
        if len(candidates) <= 1 or not self.s.llm.enabled:
            return candidates[:MAX_EVALUATE]
        pick = await self.s.llm.structured(
            careers_pick.PROMPT,
            careers_pick.CareersPick,
            site=self.site,
            links="\n".join(f"{i}. {c.text or '-'} — {c.url}" for i, c in enumerate(candidates)),
        )
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
        scan = await scan_with_render(self.s, url, required=False, render=render)
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
        tree = parse(html)
        title = _plain(node_text(tree.css_first("h1")) or node_text(tree.css_first("title")))
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

    async def sitemap(self) -> list[Candidate]:
        root = f"https://{host_of(self.site)}"
        found: dict[str, Candidate] = {}
        queue = [f"{root}/sitemap.xml", f"{root}/sitemap_index.xml"]
        fetched = 0
        while queue and fetched < MAX_SITEMAPS:
            url = queue.pop(0)
            fetched += 1
            result = await self.s.fetch(url)
            if not result.ok:
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
                    if strip_accents(seg).lower().removesuffix(".html") in _CAREERS_SEGMENTS:
                        parent = normalize_url("/" + "/".join(segs[: i + 1]), loc) or loc
                        found.setdefault(parent, Candidate(parent, "", score + 1))
                        break
            if found:
                break
        return sorted(found.values(), key=lambda c: (-c.score, len(c.url)))

    async def probe_common_paths(self) -> bool:
        root = f"https://{host_of(self.site)}"
        home = await self.s.fetch(self.site)  # đã có trong HTTP cache
        home_print = _fingerprint(home.text) if home.ok else None
        for path in COMMON_PATHS:
            candidate = Candidate(f"{root}{path}", "", 2)
            result = await self.s.fetch(candidate.url)
            # Đường dẫn không tồn tại thường bị chuyển về trang chủ, hoặc (SPA) trả nguyên khung
            # HTML của trang chủ với mã 200: soft 404, bỏ qua.
            if not result.ok or not path_segments(result.final_url):
                continue
            if home_print is not None and _fingerprint(result.text) == home_print:
                self.step(f"{candidate.url}: giống trang chủ (soft 404)")
                continue
            if await self.evaluate(candidate, hop=1, render=False):
                return True
        return False

    async def probe_subdomains(self) -> bool:
        base = registrable_domain(host_of(self.site))
        for sub in SUBDOMAINS:
            if await self.evaluate(Candidate(f"https://{sub}.{base}/", "", 2), hop=1):
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
            r.error = "không mở được trang chủ"
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
        if await self.probe_common_paths():
            r.method = "common_path"
            return r
        if await self.probe_subdomains():
            r.method = "subdomain"
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
    run = _Discovery(session, domain)
    try:
        async with asyncio.timeout(timeout):
            result = await run.run(input_url)
    except SourceError as exc:
        result = run.result
        result.error = f"{exc.outcome}: {exc}"
    except TimeoutError:
        result = run.result
        result.error = TIMEOUT
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
        source = CareersSource(url)
        if source.name not in seen:
            seen.add(source.name)
            sources.append(source)
    return sources
