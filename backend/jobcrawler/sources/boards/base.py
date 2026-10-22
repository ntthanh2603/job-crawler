"""Giao diện adapter trang tuyển dụng (TopCV, VietnamWorks, ...) cho lượt index tăng dần.

Một adapter biết bốn việc:

1. `entries()`: liệt kê URL tin đang mở (thường từ sitemap, kèm `lastmod`) — để lượt index đủ
   (ban đêm) chỉ tải tin mới hoặc đã đổi.
2. `recent()`: chỉ phần mới nhất của danh sách (sitemap mới nhất, trang đầu danh sách mới nhất),
   vài request — cho lượt index đuôi ban ngày (vài phút một lần). Không bao giờ là danh sách đủ.
3. `parse_job()`: bóc một trang chi tiết thành `RawJob` + profile nhà tuyển dụng trên trang.
4. `parse_profile()` / `profile_listing()`: đọc trang profile nhà tuyển dụng (website, MST,
   danh sách tin, kể cả các trang sau) — dùng khi map profile ↔ doanh nghiệp và khi crawl trực
   tiếp một doanh nghiệp.

Phần lớn trang tuyển dụng Việt Nam có sitemap + JSON-LD JobPosting nên `SitemapBoard` cài sẵn
các bước đó; adapter chỉ khai báo URL sitemap, mẫu URL tin/profile và phần đặc thù.
"""

from __future__ import annotations

import contextlib
import email.utils
import re
import unicodedata
from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field, field_validator
from selectolax.lexbor import LexborHTMLParser

from jobcrawler.common.logging import get_logger
from jobcrawler.common.pages import (
    Link,
    base_url,
    clean_text,
    extract_links,
    meta_content,
    next_page_urls,
    parse,
)
from jobcrawler.common.schemas import FetchOutcome, RawJob
from jobcrawler.common.text import normalize_company_name
from jobcrawler.common.urls import host_of, normalize_url, same_site
from jobcrawler.sources import jsonld
from jobcrawler.sources.base import SourceError, SourceSession, text_or_none
from jobcrawler.sources.sitemaps import parse_sitemap, sitemap_body

log = get_logger(__name__)

_SOCIAL = re.compile(
    r"(facebook|fb|linkedin|youtube|tiktok|instagram|twitter|x|zalo)\.(com|me|vn)$"
)


@dataclass(frozen=True, slots=True)
class BoardEntry:
    """Một tin trong danh sách của trang tuyển dụng (chưa tải trang chi tiết)."""

    url: str
    external_id: str
    lastmod: datetime | None = None


class ProfileRef(BaseModel):
    """Profile nhà tuyển dụng trên trang tuyển dụng (→ `source_profiles`)."""

    external_id: str
    url: str
    name: str
    website: str | None = None  # website doanh nghiệp (không phải trang trên board)
    tax_code: str | None = None
    info: dict[str, Any] = Field(default_factory=dict)  # logo, địa chỉ, quy mô...

    @field_validator("name")
    @classmethod
    def _nfc(cls, value: str) -> str:
        # Vài trang trả tiếng Việt dạng tổ hợp (NFD): chuẩn về NFC, gọn khoảng trắng.
        return " ".join(unicodedata.normalize("NFC", value).split())


@dataclass(slots=True)
class BoardJob:
    job: RawJob
    profile: ProfileRef | None


@dataclass(slots=True)
class ProfilePage:
    """Một trang danh sách tin của profile."""

    urls: list[str]  # URL tin (đã chuẩn hoá)
    next_url: str | None = None  # trang sau
    last: bool = False  # trang cuối và chắc chắn đã liệt kê đủ tin của profile


@dataclass(slots=True)
class ProfileListing:
    """Tin đang mở của một profile, đọc trực tiếp trên trang tuyển dụng."""

    urls: list[str]
    # Chắc chắn là toàn bộ tin của profile (tin đã index không có ở đây là đã đóng). Sai: chỉ
    # là một phần (trang chỉ hiện tin nổi bật, chưa biết cách phân trang...).
    complete: bool = False


@dataclass(slots=True)
class ListingState:
    """Theo dõi việc đọc danh sách: chỉ khi đọc đủ mới được coi tin vắng mặt là đã gỡ."""

    complete: bool = True
    sitemaps: int = 0  # số request danh sách (sitemap, trang danh sách, trang API)
    errors: list[str] = field(default_factory=list)

    def fail(self, message: str) -> None:
        self.complete = False
        self.errors.append(message)


# `known(mã tin)` → các mã đã có trong index. Lượt đuôi đọc danh sách mới nhất trước và dừng đọc
# trang sau khi đã tới phần đã index.
KnownIds = Callable[[Sequence[str]], Awaitable[set[str]]]

# Lệch đồng hồ giữa ta và trang: If-Modified-Since lùi lại chừng này so với lượt trước.
IF_MODIFIED_MARGIN = timedelta(minutes=5)
_OLDEST = datetime.min.replace(tzinfo=UTC)


def if_modified_since(since: datetime | None) -> dict[str, str] | None:
    """Header hỏi "có đổi từ lượt trước không" (304 = không đổi); None nếu chưa có lượt trước."""
    if since is None:
        return None
    moment = (since - IF_MODIFIED_MARGIN).astimezone(UTC)
    return {"If-Modified-Since": email.utils.format_datetime(moment, usegmt=True)}


def newest_first(entries: Iterable[BoardEntry]) -> list[BoardEntry]:
    """`lastmod` mới trước; cùng `lastmod` (hay không có) thì tin đứng sau trong danh sách trước
    (sitemap thường thêm tin mới vào cuối)."""
    indexed = list(enumerate(entries))
    indexed.sort(key=lambda item: (item[1].lastmod or _OLDEST, item[0]), reverse=True)
    return [entry for _, entry in indexed]


async def reached_known(known: KnownIds | None, external_ids: Sequence[str]) -> bool:
    """Trang danh sách (mới nhất trước) đã tới phần đã index: ít nhất nửa số tin đã có. Tính theo
    tỉ lệ chứ không theo tin cuối để tin ghim/quảng cáo đầu hay cuối trang không làm lệch."""
    if known is None or not external_ids:
        return False
    found = await known(external_ids)
    return 2 * sum(1 for i in external_ids if i in found) >= len(external_ids)


_TITLE_NOISE = re.compile(
    r"^(tìm việc làm,?\s*)?(tuyển dụng|việc làm)\s+|"
    r"\s*[-–]?\s*(tuyển dụng|thông tin công ty|việc làm)( mới nhất.*)?$",
    re.IGNORECASE,
)


def clean_profile_title(title: str) -> str | None:
    """`og:title` của trang profile → tên: bỏ "| Tên trang", "Tuyển Dụng", "Thông tin công ty"."""
    name = title.split(" | ")[0]
    for _ in range(2):
        name = _TITLE_NOISE.sub("", name).strip(" -–")
    return name or None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def is_social_host(host: str) -> bool:
    """Facebook, LinkedIn, YouTube...: không phải website doanh nghiệp."""
    return bool(_SOCIAL.search(host))


def website_url(value: Any) -> str | None:
    """Website nhà tuyển dụng tự nhập trên trang tuyển dụng, hay thiếu scheme ("www.abc.com.vn").
    None: không phải địa chỉ web ("Đang cập nhật")."""
    text = text_or_none(value) if isinstance(value, str) else None
    if not text:
        return None
    url = normalize_url(text if "://" in text else f"https://{text}")
    return url if url and "." in host_of(url) else None


def org_nodes(html: str, url: str) -> list[dict[str, Any]]:
    """`hiringOrganization` của các JobPosting trên trang."""
    found: list[dict[str, Any]] = []
    for node in jsonld.job_postings(html, url):
        org = node.get("hiringOrganization")
        if isinstance(org, list):
            org = org[0] if org else None
        if isinstance(org, dict):
            found.append(org)
        elif isinstance(org, str) and org.strip():
            found.append({"name": org.strip()})
    return found


_MAPS = re.compile(r"(maps\.google\.|google\.[a-z.]+/maps|goo\.gl/maps|maps\.app\.goo\.gl)")
_ORG_TYPES = {"organization", "corporation", "localbusiness", "employmentagency", "ngo"}


def organization_nodes(html: str, url: str) -> list[dict[str, Any]]:
    """Object Organization trong JSON-LD (kể cả `ProfilePage.mainEntity`, `@graph`)."""
    if "ld+json" not in html:
        return []
    try:
        data = jsonld._EXTRACTOR.extract(html, base_url=url)
    except Exception:
        return []
    return [n for n in jsonld._walk(data) if jsonld._types(n) & _ORG_TYPES]


def _address_text(value: Any) -> str | None:
    if isinstance(value, str):
        return text_or_none(value)
    if isinstance(value, dict):
        parts = [value.get(k) for k in ("streetAddress", "addressLocality", "addressRegion")]
        return ", ".join(p.strip() for p in parts if isinstance(p, str) and p.strip()) or None
    if isinstance(value, list) and value:
        return _address_text(value[0])
    return None


class BoardAdapter(ABC):
    """Một trang tuyển dụng. `name` là giá trị cột `source` (job_postings, source_profiles)."""

    name: ClassVar[str]
    home: ClassVar[str]  # https://www.topcv.vn/
    # Request/giây tới trang này (None = mặc định RATE_LIMIT_PER_SECOND). Robots Crawl-delay
    # vẫn được tôn trọng ở tầng mạng.
    rate: ClassVar[float | None] = None
    # Ghi chú hiển thị (vd. vì sao tắt). `enabled=False`: không chạy trong cron/`index-boards`.
    enabled: ClassVar[bool] = True
    note: ClassVar[str] = ""
    # CSS selector của link tới profile nhà tuyển dụng trên trang chi tiết tin (nếu biết).
    profile_selector: ClassVar[str | None] = None
    # CSS selector của tên nhà tuyển dụng trên trang profile (khi <h1> là banner của trang).
    profile_name_selector: ClassVar[str | None] = None
    # Trang profile có liệt kê link tin đang mở trong HTML (không: chỉ dùng tin đã index).
    profile_lists_jobs: ClassVar[bool] = True
    # Phân trang của trang profile đã kiểm bằng mẫu thật: đọc hết các trang là đủ mọi tin.
    profile_listing_complete: ClassVar[bool] = False
    # `identifier` của JobPosting là mã tin (CareerViet, CareerLink). Phần lớn trang ghi mã
    # CÔNG TY ở đó: không giữ trong tin, chỉ dùng để nhận ra link profile.
    identifier_is_job: ClassVar[bool] = False
    # Profile giữ chỗ cho tin ẩn danh / headhunter (vd. "CareerLink's client"): không phải
    # nhà tuyển dụng thật, không lưu, không map.
    placeholder_profiles: ClassVar[frozenset[str]] = frozenset()
    # Lượt đuôi: số trang danh sách / trang API tối đa (dừng sớm hơn khi tới phần đã index).
    tail_max_pages: ClassVar[int] = 5

    @property
    def source(self) -> str:
        """Tên nguồn trong `source_fetches`, log."""
        return f"board:{self.name}"

    @abstractmethod
    def job_id(self, url: str) -> str | None:
        """Mã tin trong URL trang chi tiết (None: không phải URL tin)."""

    @abstractmethod
    def profile_id(self, url: str) -> str | None:
        """Mã profile nhà tuyển dụng trong URL (None: không phải URL profile)."""

    @abstractmethod
    def entries(self, session: SourceSession, state: ListingState) -> AsyncIterator[BoardEntry]:
        """Liệt kê tin đang mở. Lỗi một phần thì `state.fail(...)`; bị chặn thì ném SourceError."""

    async def recent(
        self,
        session: SourceSession,
        state: ListingState,
        *,
        limit: int,
        since: datetime | None = None,
        known: KnownIds | None = None,
    ) -> AsyncGenerator[BoardEntry, None]:
        """Tin mới nhất trên trang (mới trước, không trùng mã), tối đa `limit` — cho lượt index
        đuôi ban ngày. Chỉ đọc vài request (xem `newest()` của từng adapter).

        Không bao giờ là danh sách đủ: `state.complete` luôn False, tin vắng mặt không bị coi là
        đã gỡ. `since`: lượt trước đã đọc xong lúc này (sitemap không đổi từ đó thì bỏ qua, 304);
        `known`: hỏi mã tin nào đã có trong index (để dừng đọc trang sau). Lỗi như `entries()`."""
        state.complete = False
        seen: set[str] = set()
        if limit <= 0:
            return
        async with contextlib.aclosing(
            self.newest(session, state, limit=limit, since=since, known=known)
        ) as found:
            async for entry in found:
                if entry.external_id in seen:
                    continue
                seen.add(entry.external_id)
                yield entry
                if len(seen) >= limit:
                    return

    async def newest(
        self,
        session: SourceSession,
        state: ListingState,
        *,
        limit: int,
        since: datetime | None,
        known: KnownIds | None,
    ) -> AsyncGenerator[BoardEntry, None]:
        """Phần mới nhất của danh sách, cho `recent()`. Mặc định: đọc cả danh sách (`entries()`)
        rồi lấy `lastmod` mới nhất — đắt, adapter nên ghi đè."""
        found = [entry async for entry in self.entries(session, state)]
        for entry in newest_first(found)[:limit]:
            yield entry

    @abstractmethod
    def parse_job(self, html: str, url: str) -> BoardJob | None:
        """Trang chi tiết → tin + profile. None: không bóc được."""

    def enrich_job(self, job: RawJob, tree: LexborHTMLParser) -> RawJob:
        """Trường trang chi tiết chỉ ghi trong HTML, không có trong JSON-LD (học vấn, danh mục
        nghề...). Mặc định không thêm gì."""
        return job

    def parse_profile(self, html: str, url: str) -> ProfileRef | None:
        """Trang profile → thông tin nhà tuyển dụng: Organization JSON-LD (tên, website, địa
        chỉ) nếu có, không thì tên từ <h1>."""
        url = self.canonical(url)
        external_id = self.profile_id(url)
        if external_id is None:
            return None
        info: dict[str, Any] = {}
        name, website = None, None
        for org in organization_nodes(html, url):
            urls = [org.get("url"), *(v for v in _as_list(org.get("sameAs")))]
            # Organization của chính trang tuyển dụng (JobsGO ghi trên mọi trang): bỏ.
            own_url = normalize_url(str(org.get("url") or ""), self.home)
            if own_url and same_site(own_url, self.home) and self.canonical(own_url) != url:
                continue
            name = text_or_none(org.get("legalName")) or text_or_none(org.get("name"))
            for value in urls:
                candidate = normalize_url(value, self.home) if isinstance(value, str) else None
                if (
                    candidate
                    and not same_site(candidate, self.home)
                    and not is_social_host(host_of(candidate))
                    and not _MAPS.search(candidate)
                ):
                    website = candidate
                    break
            if address := _address_text(org.get("address")):
                info["address"] = address
            break
        if not name:
            name = self.profile_name(parse(html))
        if not name:
            return None
        return ProfileRef(external_id=external_id, url=url, name=name, website=website, info=info)

    def profile_name(self, tree: LexborHTMLParser) -> str | None:
        """Tên nhà tuyển dụng trên trang profile khi không có Organization JSON-LD: selector riêng
        → <h1> → `og:title` đã bỏ phần quảng cáo của trang ("... Tuyển Dụng | Vieclam24h")."""
        for selector in (self.profile_name_selector, "h1"):
            node = tree.css_first(selector) if selector else None
            if node is not None and (text := clean_text(node.text(deep=True, separator=" "))):
                return text
        og = meta_content(tree, "og:title")
        return clean_profile_title(og) if og else None

    def profile_jobs_url(self, profile_url: str) -> str:
        """Trang liệt kê tin đang mở của một profile (mặc định: chính trang profile)."""
        return profile_url

    def canonical(self, url: str) -> str:
        """URL dùng làm khoá: bỏ fragment và query (tham số theo dõi như `?utm=`,
        `?lab_feature=`). Adapter có mã tin trong query thì ghi đè."""
        normalized = normalize_url(url) or url
        return urlunsplit(urlsplit(normalized)._replace(query="", fragment=""))

    def profile_job_urls(self, html: str, url: str) -> list[str]:
        """Link tin trên trang profile (tin đang mở của nhà tuyển dụng), đã chuẩn hoá."""
        links = extract_links(parse(html), url)
        urls = (self.canonical(link.url) for link in links)
        return list(dict.fromkeys(u for u in urls if self.job_id(u)))

    def profile_page(self, html: str, url: str) -> ProfilePage:
        """Một trang danh sách tin của profile: link tin + trang sau (link `?page=N` cùng đường
        dẫn)."""
        pages = next_page_urls(extract_links(parse(html), url), url)
        next_url = pages[0] if pages else None
        return ProfilePage(
            urls=self.profile_job_urls(html, url),
            next_url=next_url,
            last=next_url is None and self.profile_listing_complete,
        )

    async def profile_listing(
        self, session: SourceSession, profile_url: str, *, max_pages: int
    ) -> ProfileListing:
        """Mọi tin đang mở của profile: đọc trang danh sách tin rồi các trang sau (tối đa
        `max_pages`). Trang đầu lỗi thì ném `SourceError`; trang sau lỗi hoặc chạm giới hạn thì
        trả phần đã đọc (`complete=False`) và đánh dấu nguồn chưa xong."""
        url: str | None = self.profile_jobs_url(profile_url)
        found: dict[str, None] = {}
        for i in range(max_pages):
            assert url is not None
            result = await (
                session.fetch_ok(url, rate=self.rate)
                if i == 0
                else session.fetch(url, rate=self.rate)
            )
            if not result.ok:
                session.truncate("profile_pages")
                return ProfileListing(list(found))
            page = self.profile_page(result.text, result.final_url)
            fresh = [u for u in page.urls if u not in found]
            found.update(dict.fromkeys(fresh))
            session.count(result, len(page.urls))
            if page.next_url is None:
                return ProfileListing(list(found), complete=page.last)
            if not fresh:
                # Trang sau lặp lại trang trước (tham số trang bị bỏ qua): dừng.
                return ProfileListing(list(found))
            url = page.next_url
        session.truncate("profile_pages")
        return ProfileListing(list(found))

    def find_profile_link(
        self,
        tree: LexborHTMLParser,
        links: list[Link],
        org_name: str | None,
        org_id: str | None = None,
        *,
        page_url: str | None = None,
    ) -> str | None:
        """URL profile nhà tuyển dụng trên trang chi tiết tin (`page_url`: URL trang, để hiểu link
        tương đối).

        Trang tin thường có nhiều link tới profile khác (menu "nhà tuyển dụng hàng đầu", tin
        tương tự), nên: selector riêng của adapter → link có chữ khớp tên nhà tuyển dụng →
        chỉ có đúng một profile được nhắc tới.
        """
        if self.profile_selector:
            node = tree.css_first(self.profile_selector)
            href = node.attributes.get("href") if node is not None else None
            if href:
                url = normalize_url(href, base_url(tree, page_url) if page_url else self.home)
                if url and self.profile_id(url):
                    return self.canonical(url)
        candidates = [(self.canonical(link.url), link.text) for link in links]
        candidates = [(u, t) for u, t in candidates if self.profile_id(u)]
        if org_id:
            for url, _ in candidates:
                if self.profile_id(url) == org_id:
                    return url
        if org_name:
            want = normalize_company_name(org_name)
            for url, label in candidates:
                if label and normalize_company_name(label) == want:
                    return url
        distinct = {u for u, _ in candidates}
        return distinct.pop() if len(distinct) == 1 else None


_NUMBER = re.compile(r"(\d+)(?:\.xml)?(?:\.gz)?$")
MAX_TAIL_SITEMAPS = 10  # lượt đuôi: chặn trên số sitemap đọc (index lồng nhau bất thường)


class SitemapBoard(BoardAdapter):
    """Trang tuyển dụng có sitemap tin (kèm `lastmod`) và JSON-LD JobPosting ở trang chi tiết.

    Lượt đuôi (`newest()`): đọc `tail_sitemaps` (mặc định chính `sitemaps`); gặp sitemap index thì
    chỉ đi xuống `tail_files` sitemap tin mới nhất (theo `lastmod` của index; index không ghi thì
    các file số lớn nhất — tin mới thường được thêm vào cuối), rồi xếp tin theo `lastmod` mới
    trước."""

    sitemaps: ClassVar[tuple[str, ...]]  # sitemap gốc (index hoặc sitemap tin)
    job_sitemap: ClassVar[re.Pattern[str]]  # sitemap con chứa URL tin
    job_url: ClassVar[re.Pattern[str]]  # URL tin (đã chuẩn hoá), nhóm `id`
    profile_url: ClassVar[re.Pattern[str]]  # URL profile nhà tuyển dụng, nhóm `id`
    max_sitemaps: ClassVar[int] = 400
    # Một số trang ghi `lastmod` = giờ sinh sitemap cho mọi URL: vô dụng, bỏ qua.
    trust_lastmod: ClassVar[bool] = True
    # Tiêu đề lấy từ trang thay vì JSON-LD (vd. JSON-LD đã bỏ dấu tiếng Việt).
    title_selector: ClassVar[str | None] = None
    # Lượt đuôi: sitemap đọc thẳng (vd. sitemap tin trong ngày, trang đầu sitemap xếp mới nhất).
    tail_sitemaps: ClassVar[tuple[str, ...]] = ()
    # Lượt đuôi: mỗi sitemap index chỉ đọc chừng này sitemap tin mới nhất.
    tail_files: ClassVar[int] = 2
    # Lượt đuôi: hỏi If-Modified-Since theo lượt trước (sitemap tĩnh có Last-Modified: 304 = không
    # đổi, khỏi tải lại cả file). Chỉ bật khi `tail_sitemaps` là sitemap tin (không phải index:
    # index không đổi chưa chắc sitemap con không đổi).
    tail_conditional: ClassVar[bool] = False

    def job_id(self, url: str) -> str | None:
        m = self.job_url.search(self.canonical(url))
        return m.group("id") if m else None

    def profile_id(self, url: str) -> str | None:
        m = self.profile_url.search(self.canonical(url))
        return m.group("id") if m else None

    async def _sitemap(self, session: SourceSession, url: str) -> str | None:
        result = await session.fetch(url, rate=self.rate, use_cache=False)
        if not result.ok:
            return None
        return sitemap_body(result.content)

    def sitemap_entries(self, items: list[tuple[str, datetime | None]]) -> list[BoardEntry]:
        """URL tin trong một sitemap tin (đã chuẩn hoá; bỏ URL không phải tin)."""
        found: list[BoardEntry] = []
        for loc, lastmod in items:
            canonical = self.canonical(loc)
            if (external_id := self.job_id(canonical)) is not None:
                found.append(
                    BoardEntry(
                        url=canonical,
                        external_id=external_id,
                        lastmod=lastmod if self.trust_lastmod else None,
                    )
                )
        return found

    async def entries(
        self, session: SourceSession, state: ListingState
    ) -> AsyncIterator[BoardEntry]:
        queue = list(self.sitemaps)
        seen: set[str] = set()
        while queue:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            if state.sitemaps >= self.max_sitemaps:
                state.fail(f"vượt {self.max_sitemaps} sitemap")
                return
            state.sitemaps += 1
            body = await self._sitemap(session, url)
            if body is None:
                if url in self.sitemaps:
                    raise SourceError(FetchOutcome.ERROR, f"không đọc được sitemap gốc {url}")
                state.fail(f"sitemap lỗi: {url}")
                continue
            is_index, items = parse_sitemap(body)
            if is_index:
                queue.extend(loc for loc, _ in items if self.job_sitemap.search(loc))
                continue
            for entry in self.sitemap_entries(items):
                yield entry

    def newest_sitemaps(self, items: list[tuple[str, datetime | None]]) -> list[str]:
        """`tail_files` sitemap tin mới nhất trong một sitemap index, cũ trước mới sau (tin đọc
        sau được coi là mới hơn khi cùng `lastmod`)."""
        children = [(loc, lastmod) for loc, lastmod in items if self.job_sitemap.search(loc)]
        dated = {lastmod for _, lastmod in children if lastmod is not None}
        if len(dated) > 1 and all(lastmod is not None for _, lastmod in children):
            children.sort(key=lambda item: item[1] or _OLDEST)
            return [loc for loc, _ in children[-self.tail_files :]]
        # Không có `lastmod` riêng từng file: file số lớn nhất (thứ tự trong index nếu không số).
        numbered = [(m, loc) for loc, _ in children if (m := _NUMBER.search(loc))]
        locs = [loc for loc, _ in children]
        if len(numbered) == len(children):
            locs = [loc for _, loc in sorted(numbered, key=lambda item: int(item[0].group(1)))]
        return locs[-self.tail_files :]

    async def newest(
        self,
        session: SourceSession,
        state: ListingState,
        *,
        limit: int,
        since: datetime | None,
        known: KnownIds | None,
    ) -> AsyncGenerator[BoardEntry, None]:
        roots = self.tail_sitemaps or self.sitemaps
        headers = if_modified_since(since) if self.tail_conditional else None
        queue = list(roots)
        seen: set[str] = set()
        found: list[BoardEntry] = []
        read = 0
        while queue:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            if len(seen) > MAX_TAIL_SITEMAPS:
                state.fail(f"vượt {MAX_TAIL_SITEMAPS} sitemap ở lượt đuôi")
                break
            state.sitemaps += 1
            result = await session.fetch(url, rate=self.rate, use_cache=False, headers=headers)
            if result.status == 304:
                read += 1  # không đổi từ lượt trước: không có tin mới
                continue
            body = sitemap_body(result.content) if result.ok else None
            if body is None:
                state.fail(f"sitemap lỗi: {url}")
                continue
            read += 1
            is_index, items = parse_sitemap(body)
            if is_index:
                queue.extend(self.newest_sitemaps(items))
            else:
                found.extend(self.sitemap_entries(items))
        if not read:
            raise SourceError(FetchOutcome.ERROR, f"không đọc được sitemap {', '.join(roots)}")
        for entry in newest_first(found)[:limit]:
            yield entry

    # --- trang chi tiết ---

    def profile_from_page(
        self,
        tree: LexborHTMLParser,
        links: list[Link],
        orgs: list[dict[str, Any]],
        org_id: str | None = None,
        *,
        page_url: str | None = None,
    ) -> ProfileRef | None:
        """Profile từ link trên trang + `hiringOrganization` (tên, website, logo)."""
        org = orgs[0] if orgs else {}
        org_name = text_or_none(org.get("name"))
        # `hiringOrganization.sameAs/url` trỏ về trang profile trên chính trang tuyển dụng là
        # nguồn chắc nhất (TopDev, JobsGO); không có thì tìm link trên trang.
        url = next(
            (u for u in self._org_urls(org) if same_site(u, self.home) and self.profile_id(u)),
            None,
        ) or self.find_profile_link(tree, links, org_name, org_id, page_url=page_url)
        if url is None:
            return None
        label = next((link.text for link in links if self.canonical(link.url) == url), "")
        name = org_name or clean_text(label)
        external_id = self.profile_id(url)
        if not name or external_id is None or external_id in self.placeholder_profiles:
            return None
        # sameAs/url trỏ về chính trang tuyển dụng hay mạng xã hội thì không phải website.
        website = next(
            (
                u
                for u in self._org_urls(org)
                if not same_site(u, self.home) and not is_social_host(host_of(u))
            ),
            None,
        )
        logo = org.get("logo")
        if isinstance(logo, dict):
            logo = logo.get("url")
        info = {"logo": logo} if isinstance(logo, str) and logo else {}
        return ProfileRef(external_id=external_id, url=url, name=name, website=website, info=info)

    def _org_urls(self, org: dict[str, Any]) -> list[str]:
        found: list[str] = []
        for key in ("sameAs", "url"):
            values = org.get(key)
            for value in values if isinstance(values, list) else [values]:
                if isinstance(value, str) and (u := normalize_url(value, self.home)):
                    found.append(self.canonical(u) if same_site(u, self.home) else u)
        return found

    def parse_job(self, html: str, url: str) -> BoardJob | None:
        url = self.canonical(url)
        external_id = self.job_id(url)
        jobs = jsonld.raw_jobs(html, url, source=self.name)
        if external_id is None or not jobs:
            return None
        tree = parse(html)
        updates: dict[str, Any] = {"external_id": external_id, "url": url}
        node = tree.css_first(self.title_selector) if self.title_selector else None
        if node is not None and (title := clean_text(node.text(deep=True, separator=" "))):
            updates["title"] = title
        identifier = jobs[0].extra.get("identifier")
        if not self.identifier_is_job and identifier is not None:
            updates["extra"] = {k: v for k, v in jobs[0].extra.items() if k != "identifier"}
        job = self.enrich_job(jobs[0].model_copy(update=updates), tree)
        links = extract_links(tree, url)
        org_id = None if self.identifier_is_job or identifier is None else str(identifier)
        profile = self.profile_from_page(tree, links, org_nodes(html, url), org_id, page_url=url)
        if profile is not None and not job.company_name:
            job = job.model_copy(update={"company_name": profile.name})
        return BoardJob(job=job, profile=profile)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name} {host_of(self.home)}>"


class ListingBoard(SitemapBoard):
    """Trang không có sitemap dùng được: đọc trang danh sách (mới nhất trước) cho tới khi một
    trang không còn tin mới. Đi hết danh sách mới coi là đọc đủ.

    Lượt đuôi: các trang đầu của chính danh sách đó, dừng khi đã tới phần đã index (`known`: nửa
    trang trở lên đã có), khi trang không còn tin mới, hoặc sau `tail_max_pages` trang."""

    sitemaps = ()
    job_sitemap = re.compile(r"$^")
    listing_url: ClassVar[str]  # có {page}
    first_page: ClassVar[int] = 1
    max_listing_pages: ClassVar[int] = 1_000
    # Trang chỉ cho xem N trang đầu (Vieclam24h, 123job): hết trang không có nghĩa là hết tin,
    # nên không bao giờ coi là đọc đủ.
    listing_complete: ClassVar[bool] = True

    def listing_job_urls(self, html: str, url: str) -> list[str]:
        links = extract_links(parse(html), url)
        return list(
            dict.fromkeys(u for u in (self.canonical(x.url) for x in links) if self.job_id(u))
        )

    async def _listing_page(
        self, session: SourceSession, state: ListingState, page: int, seen: set[str]
    ) -> list[BoardEntry] | None:
        """Tin chưa gặp trong lượt trên một trang danh sách. None: trang lỗi (trang đầu lỗi thì
        ném SourceError)."""
        url = self.listing_url.format(page=page)
        result = await session.fetch(url, rate=self.rate, use_cache=False)
        state.sitemaps += 1
        if not result.ok:
            if page == self.first_page:
                raise SourceError(FetchOutcome.ERROR, f"không đọc được danh sách {url}")
            state.fail(f"trang danh sách lỗi: {url}")
            return None
        fresh: list[BoardEntry] = []
        for job_url in self.listing_job_urls(result.text, result.final_url):
            external_id = self.job_id(job_url)
            if external_id is not None and external_id not in seen:
                seen.add(external_id)
                fresh.append(BoardEntry(url=job_url, external_id=external_id))
        session.count(result, len(fresh))
        return fresh

    async def entries(
        self, session: SourceSession, state: ListingState
    ) -> AsyncIterator[BoardEntry]:
        seen: set[str] = set()
        for page in range(self.first_page, self.first_page + self.max_listing_pages):
            fresh = await self._listing_page(session, state, page, seen)
            if fresh is None:
                return
            for entry in fresh:
                yield entry
            if not fresh:
                break  # hết danh sách
        else:
            state.fail(f"vượt {self.max_listing_pages} trang danh sách")
            return
        if not self.listing_complete:
            state.fail("trang chỉ cho xem một phần danh sách")

    async def newest(
        self,
        session: SourceSession,
        state: ListingState,
        *,
        limit: int,
        since: datetime | None,
        known: KnownIds | None,
    ) -> AsyncGenerator[BoardEntry, None]:
        seen: set[str] = set()
        for page in range(self.first_page, self.first_page + self.tail_max_pages):
            fresh = await self._listing_page(session, state, page, seen)
            if not fresh:
                return  # trang lỗi hoặc hết danh sách
            for entry in fresh:
                yield entry
            if len(seen) >= limit or await reached_known(known, [e.external_id for e in fresh]):
                return
