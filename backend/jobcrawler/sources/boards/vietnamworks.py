"""VietnamWorks (vietnamworks.com): không có sitemap (URL trong robots.txt trả 404).

- Danh sách: API tìm kiếm mà chính trang gọi, `POST ms.vietnamworks.com/job-search/v1.0/search`
  (không cần đăng nhập; host này không có robots.txt), sắp theo `approvedOn` giảm dần, 100 tin
  mỗi trang, `lastUpdatedOn` làm `lastmod`. Chỉ xin các trường cần (không xin email/liên hệ).
- Lượt đuôi: các trang đầu của chính API đó (mới duyệt trước), dừng khi nửa trang đã có trong
  index: thường 1 request (~15 KB), tối đa 3.
- Trang chi tiết `/<alias>-<id>-jv` qua HTTP không có JSON-LD: dữ liệu tin nằm trong payload
  React (`self.__next_f.push`) → bóc object có đúng `jobId`, chỉ lấy các trường cần (bỏ
  `emailAddress`, `contactName`). Trang đã render (trình duyệt) có JSON-LD thì dùng JSON-LD,
  nhưng `baseSalary` ở đó sai (luôn USD/HOUR) nên bỏ số, chỉ giữ chữ.
- Profile `/nha-tuyen-dung/<slug>-c<companyId>`: `__NEXT_DATA__.companyProfile` (địa chỉ,
  website thường trống); không có MST. Trang profile chỉ hiện 20 tin đầu (`companyJobs`): danh
  sách tin của profile lấy bằng chính API tìm kiếm, lọc `companyId`.
Mẫu: tests/fixtures/boards/vietnamworks/.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import datetime
from typing import Any

from jobcrawler.common.pages import parse
from jobcrawler.common.schemas import ExtractionMethod, FetchOutcome, RawJob
from jobcrawler.common.text import strip_accents
from jobcrawler.common.urls import normalize_url
from jobcrawler.sources.base import (
    SourceError,
    SourceSession,
    join_nonempty,
    parse_datetime,
    text_or_none,
)
from jobcrawler.sources.boards.base import (
    BoardEntry,
    BoardJob,
    KnownIds,
    ListingState,
    ProfileListing,
    ProfileRef,
    SitemapBoard,
    reached_known,
)

API = "https://ms.vietnamworks.com/job-search/v1.0/search"
PAGE_SIZE = 100
MAX_PAGES = 300
JSON_HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}
_PUSH = re.compile(r'self\.__next_f\.push\(\[1,\s*("(?:[^"\\]|\\.)*")\]\)')
_DECODER = json.JSONDecoder()
_REF = re.compile(r"^\$([0-9a-f]+)$")
# Trường có thể là tham chiếu tới dòng khác của payload.
_REF_FIELDS = (
    "jobDescription",
    "jobRequirement",
    "skills",
    "benefits",
    "workingLocations",
    "companyInfo",
)


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", strip_accents(text).lower()).strip("-")


def flight_payload(html: str) -> str:
    """Nội dung các khúc `self.__next_f.push([1, "..."])` nối lại."""
    parts: list[str] = []
    for chunk in _PUSH.findall(html):
        try:
            parts.append(json.loads(chunk))
        except ValueError:
            continue
    return "".join(parts)


def rsc_rows(payload: str) -> dict[str, Any]:
    """Các dòng của payload React Server Components được tham chiếu bằng `"$<id>"`:
    dòng văn bản `<id>:T<độ dài hex theo byte UTF-8>,<nội dung>` (phân tách theo độ dài, không
    có xuống dòng sau nội dung) và dòng JSON `<id>:[...]` / `<id>:{...}` (kết thúc bằng xuống
    dòng). Dòng khác (import, gợi ý tải...) bỏ qua."""
    data = payload.encode("utf-8")
    rows: dict[str, Any] = {}
    pos = 0
    while pos < len(data):
        colon = data.find(b":", pos)
        if colon < 0:
            break
        row_id = data[pos:colon]
        kind = data[colon + 1 : colon + 2]
        if row_id.isalnum() and kind == b"T":
            comma = data.find(b",", colon + 2)
            try:
                length = int(data[colon + 2 : comma], 16)
            except ValueError:
                length = -1
            if comma > 0 and length >= 0:
                end = comma + 1 + length
                rows[row_id.decode()] = data[comma + 1 : end].decode("utf-8", errors="replace")
                pos = end
                continue
        newline = data.find(b"\n", colon)
        stop = newline if newline >= 0 else len(data)
        if row_id.isalnum() and kind in (b"[", b"{"):
            with contextlib.suppress(ValueError):
                rows[row_id.decode()] = json.loads(data[colon + 1 : stop])
        if newline < 0:
            break
        pos = newline + 1
    return rows


def resolve(rows: dict[str, Any], value: Any, depth: int = 4) -> Any:
    """Giá trị `"$27"` trỏ tới dòng `27` của payload; mảng/object thì giải từng phần tử (mảng
    kỹ năng, địa điểm thường là mảng các tham chiếu)."""
    if depth <= 0:
        return value
    if isinstance(value, str) and (m := _REF.match(value)) is not None:
        value = rows.get(m.group(1), value)
        if isinstance(value, str):
            return value
    if isinstance(value, list):
        return [resolve(rows, v, depth - 1) for v in value]
    if isinstance(value, dict):
        return {k: resolve(rows, v, depth - 1) for k, v in value.items()}
    return value


def find_object(payload: str, key: str, value: Any, *, require: str) -> dict[str, Any] | None:
    """Object JSON trong payload có `key == value` và có khoá `require`. Cùng một tin có thể
    xuất hiện nhiều lần (bản tóm tắt cắt mô tả ~300 ký tự, bản đầy đủ): lấy bản có nhiều nội dung
    nhất."""
    needle = f'"{key}":{json.dumps(value)}'
    rows = rsc_rows(payload) if '"$' in payload else {}
    best: dict[str, Any] | None = None
    best_size = -1
    start = 0
    while (i := payload.find(needle, start)) >= 0:
        start = i + 1
        # Lùi dần tới dấu `{` mở object chứa khoá này.
        pos = i
        for _ in range(400):
            pos = payload.rfind("{", 0, pos)
            if pos < 0:
                break
            try:
                obj, end = _DECODER.raw_decode(payload, pos)
            except ValueError:
                continue
            if end > i and isinstance(obj, dict) and obj.get(key) == value and require in obj:
                for field in _REF_FIELDS:
                    if field in obj:
                        obj[field] = resolve(rows, obj[field])
                size = sum(len(v) for v in obj.values() if isinstance(v, str))
                if size > best_size:
                    best, best_size = obj, size
                break
    return best


def _skills(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    names = [s.get("skillName") if isinstance(s, dict) else s for s in value]
    return [n.strip() for n in names if isinstance(n, str) and n.strip()]


def _benefits(value: Any) -> str | None:
    if not isinstance(value, list):
        return None
    items = [
        join_nonempty([b.get("benefitName"), b.get("benefitValue")], ": ")
        for b in value
        if isinstance(b, dict)
    ]
    items = [i for i in items if i]
    return "<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul>" if items else None


def _locations(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    found = [
        join_nonempty([w.get("address"), w.get("cityNameVI") or w.get("cityName")])
        for w in value
        if isinstance(w, dict)
    ]
    return list(dict.fromkeys(x for x in found if x))


class VietnamWorks(SitemapBoard):
    name = "vietnamworks"
    home = "https://www.vietnamworks.com/"
    note = "danh sách qua API tìm kiếm của trang"
    sitemaps = ()
    job_sitemap = re.compile(r"$^")
    job_url = re.compile(r"^https://www\.vietnamworks\.com/[^/]+-(?P<id>\d+)-jv$")
    profile_url = re.compile(
        r"^https://www\.vietnamworks\.com/(?:nha-tuyen-dung|companies)/[^/]+-c(?P<id>\d+)$"
    )
    tail_max_pages = 3

    @staticmethod
    def _search_body(page: int, filters: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return {
            "userId": 0,
            "query": "",
            "filter": filters or [],
            "ranges": [],
            "order": [{"field": "approvedOn", "value": "desc"}],
            "hitsPerPage": PAGE_SIZE,
            "page": page,
            # `jobUrl` chỉ được điền khi xin kèm `alias`.
            "retrieveFields": ["jobId", "alias", "jobUrl", "lastUpdatedOn"],
        }

    def _hit_url(self, hit: dict[str, Any]) -> str:
        url = str(hit.get("jobUrl") or "")
        if not url and hit.get("alias") and hit.get("jobId"):
            url = f"{self.home}{hit['alias']}-{hit['jobId']}-jv"
        return self.canonical(url)

    async def profile_listing(
        self, session: SourceSession, profile_url: str, *, max_pages: int
    ) -> ProfileListing:
        """Tin của một nhà tuyển dụng: API tìm kiếm lọc theo `companyId` (đủ mọi tin, trang
        profile chỉ hiện 20 tin đầu)."""
        company_id = self.profile_id(profile_url)
        if company_id is None:
            return await super().profile_listing(session, profile_url, max_pages=max_pages)
        filters = [{"field": "companyId", "value": company_id}]
        found: dict[str, None] = {}
        for page in range(max_pages):
            fetch = session.fetch_ok if page == 0 else session.fetch
            result = await fetch(
                API,
                method="POST",
                json=self._search_body(page, filters),
                headers=JSON_HEADERS,
                rate=self.rate,
            )
            try:
                data = result.json() if result.ok else None
            except ValueError:
                data = None
            if not isinstance(data, dict):
                if page == 0:
                    raise SourceError(FetchOutcome.ERROR, f"API tin của nhà tuyển dụng lỗi: {API}")
                session.truncate("profile_pages")
                return ProfileListing(list(found))
            hits = data.get("data") or []
            urls = [u for u in (self._hit_url(h) for h in hits) if self.job_id(u)]
            found.update(dict.fromkeys(urls))
            session.count(result, len(urls))
            meta = data.get("meta") or {}
            if not hits or page + 1 >= int(meta.get("nbPages") or 0):
                total = int(meta.get("nbHits") or 0)
                return ProfileListing(list(found), complete=len(found) >= total)
        session.truncate("profile_pages")
        return ProfileListing(list(found))

    async def _search_page(
        self, session: SourceSession, state: ListingState, page: int
    ) -> tuple[list[BoardEntry], int] | None:
        """Một trang API danh sách: (tin, tổng số trang). None: trang sau trang đầu lỗi (trang
        đầu lỗi thì ném SourceError)."""
        result = await session.fetch(
            API,
            method="POST",
            json=self._search_body(page),
            headers=JSON_HEADERS,
            rate=self.rate,
        )
        state.sitemaps += 1
        try:
            data = result.json() if result.ok else None
        except ValueError:
            data = None
        if not isinstance(data, dict):
            if page == 0:
                raise SourceError(FetchOutcome.ERROR, f"API danh sách lỗi: {result.error}")
            state.fail(f"API trang {page} lỗi")
            return None
        hits = data.get("data") or []
        if hits and not any(hit.get("jobUrl") or hit.get("alias") for hit in hits):
            raise SourceError(FetchOutcome.ERROR, "API không trả URL tin (đổi định dạng?)")
        found: list[BoardEntry] = []
        for hit in hits:
            url = self._hit_url(hit)
            if (external_id := self.job_id(url)) is not None:
                found.append(
                    BoardEntry(
                        url=url,
                        external_id=external_id,
                        lastmod=parse_datetime(hit.get("lastUpdatedOn")),
                    )
                )
        session.count(result, len(found))
        return found, int((data.get("meta") or {}).get("nbPages") or 0)

    async def entries(
        self, session: SourceSession, state: ListingState
    ) -> AsyncIterator[BoardEntry]:
        for page in range(MAX_PAGES):
            got = await self._search_page(session, state, page)
            if got is None:
                return
            found, pages = got
            for entry in found:
                yield entry
            if not found or page + 1 >= pages:
                return
        state.fail(f"vượt {MAX_PAGES} trang API")

    async def newest(
        self,
        session: SourceSession,
        state: ListingState,
        *,
        limit: int,
        since: datetime | None,
        known: KnownIds | None,
    ) -> AsyncGenerator[BoardEntry, None]:
        seen = 0
        for page in range(self.tail_max_pages):
            got = await self._search_page(session, state, page)
            if got is None:
                return
            found, pages = got
            for entry in found:
                yield entry
            seen += len(found)
            if (
                not found
                or page + 1 >= pages
                or seen >= limit
                or await reached_known(known, [e.external_id for e in found])
            ):
                return

    def parse_job(self, html: str, url: str) -> BoardJob | None:
        url = self.canonical(url)
        external_id = self.job_id(url)
        if external_id is None:
            return None
        payload = flight_payload(html)
        obj = (
            find_object(payload, "jobId", int(external_id), require="jobTitle") if payload else None
        )
        if obj is None:
            # Trang đã render bằng trình duyệt: JSON-LD (lương trong đó sai đơn vị).
            parsed = super().parse_job(html, url)
            if parsed is None:
                return None
            job = parsed.job.model_copy(
                update={
                    "salary_min": None,
                    "salary_max": None,
                    "salary_currency": None,
                    "salary_period": None,
                }
            )
            return BoardJob(job=job, profile=parsed.profile)
        return self._from_payload(obj, url, external_id)

    def _from_payload(self, obj: dict[str, Any], url: str, external_id: str) -> BoardJob | None:
        title = text_or_none(obj.get("jobTitle"))
        if not title:
            return None
        visible = bool(obj.get("isSalaryVisible"))
        low = obj.get("salaryMin") or None
        high = obj.get("salaryMax") or None
        company = text_or_none(obj.get("companyName"))
        company_id = obj.get("companyId")
        raw_info = obj.get("companyInfo")
        info: dict[str, Any] = raw_info if isinstance(raw_info, dict) else {}
        job = RawJob(
            source=self.name,
            external_id=external_id,
            url=url,
            title=title,
            company_name=company,
            description=text_or_none(obj.get("jobDescription")),
            requirements=text_or_none(obj.get("jobRequirement")),
            benefits=_benefits(obj.get("benefits")),
            locations=_locations(obj.get("workingLocations")),
            level=text_or_none(obj.get("jobLevelVI")),
            salary_raw=text_or_none(obj.get("prettySalary")),
            salary_min=float(low) if visible and low else None,
            salary_max=float(high) if visible and high else None,
            salary_currency=text_or_none(obj.get("salaryCurrency")) if visible else None,
            salary_period="MONTH" if visible and obj.get("salaryPeriodId") == 1 else None,
            skills=_skills(obj.get("skills")),
            posted_at=parse_datetime(obj.get("approvedOn")),
            posted_raw=text_or_none(obj.get("approvedOn")),
            expires_at=parse_datetime(obj.get("expiredOn")),
            expires_raw=text_or_none(obj.get("expiredOn")),
            apply_url=url,
            method=ExtractionMethod.PARSER,
        )
        profile = None
        if company and company_id:
            website = text_or_none(info.get("website"))
            profile = ProfileRef(
                external_id=str(company_id),
                url=f"{self.home}nha-tuyen-dung/{slugify(company) or 'cong-ty'}-c{company_id}",
                name=company,
                website=normalize_url(website) if website else None,
                info={"address": info["address"]} if text_or_none(info.get("address")) else {},
            )
        return BoardJob(job=job, profile=profile)

    def parse_profile(self, html: str, url: str) -> ProfileRef | None:
        url = self.canonical(url)
        external_id = self.profile_id(url)
        node = parse(html).css_first("script#__NEXT_DATA__")
        if external_id is None or node is None:
            return super().parse_profile(html, url)
        try:
            data = json.loads(node.text())
        except ValueError:
            return super().parse_profile(html, url)
        props = (data.get("props") or {}).get("pageProps") or {}
        company = props.get("companyProfile") or {}
        name = text_or_none(company.get("companyName"))
        if not name:
            return super().parse_profile(html, url)
        website = text_or_none(company.get("website"))
        address = text_or_none(company.get("address"))
        return ProfileRef(
            external_id=external_id,
            url=url,
            name=name,
            website=normalize_url(website) if website else None,
            info={"address": address} if address else {},
        )
