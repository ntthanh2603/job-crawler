"""Workday: API JSON mà chính trang `*.myworkdayjobs.com` gọi (CXS).

- Danh sách: `POST https://{host}/wday/cxs/{tenant}/{site}/jobs`
  body `{"appliedFacets": {}, "limit": 20, "offset": N, "searchText": ""}`.
- Chi tiết: `GET https://{host}/wday/cxs/{tenant}/{site}{externalPath}`.

Bảng của tập đoàn toàn cầu có hàng trăm tin: khi vượt sức chứa (số trang × 20) mà facet địa
điểm có giá trị ở Việt Nam thì lọc theo facet đó. Mẫu: tests/fixtures/workday/.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any

from jobcrawler.schemas import ExtractionMethod, RawJob
from jobcrawler.sources.ats.ref import AtsRef, AtsSpec
from jobcrawler.sources.base import (
    Source,
    SourceSession,
    gather_all,
    mentions_vietnam,
    parse_datetime,
    text_or_none,
    vietnam_first,
)

KIND = "workday"
PAGE_SIZE = 20
JSON_HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}
_HOST = r"(?P<host>(?P<tenant>[a-z0-9-]+)\.(?P<wd>wd\d+)\.myworkdayjobs\.com)"
_RESERVED_SITES = frozenset({"wday", "job", "jobs", "login", "refreshfacet"})

PATTERNS = (
    re.compile(_HOST + r"/wday/cxs/[a-z0-9-]+/(?P<site>[A-Za-z0-9_-]+)", re.IGNORECASE),
    # Có thể có locale: /en-US/External, /vi-VN/External.
    re.compile(_HOST + r"/(?:[a-z]{2}(?:-[A-Za-z]{2})?/)?(?P<site>[A-Za-z0-9_-]+)", re.IGNORECASE),
)


def make_ref(m: re.Match[str]) -> AtsRef | None:
    site = m["site"]
    if site.lower() in _RESERVED_SITES:
        return None
    host, tenant = m["host"].lower(), m["tenant"].lower()
    return AtsRef(
        kind=KIND,
        key=f"{tenant}/{site.lower()}",
        url=f"https://{host}/{site}",
        params={"host": host, "tenant": tenant, "site": site},
    )


def vietnam_facets(facets: list[dict[str, Any]] | None) -> dict[str, list[str]]:
    """Giá trị facet (thường là `locations` hoặc `locationCountry`) nằm ở Việt Nam."""
    applied: dict[str, list[str]] = {}

    def walk(nodes: list[dict[str, Any]], parameter: str | None) -> None:
        for node in nodes:
            param = node.get("facetParameter") or parameter
            values = node.get("values")
            if isinstance(values, list):
                walk(values, param)
            elif param and node.get("id") and mentions_vietnam(node.get("descriptor")):
                applied.setdefault(param, []).append(str(node["id"]))

    walk(facets or [], None)
    # Có facet quốc gia thì dùng nó (gọn hơn liệt kê từng thành phố).
    country = {k: v for k, v in applied.items() if "country" in k.lower()}
    return country or applied


def parse_listing(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for p in data.get("jobPostings") or [] if p.get("externalPath") and p.get("title")]


def parse_detail(
    data: dict[str, Any], *, ref: AtsRef, listing: dict[str, Any] | None = None
) -> RawJob | None:
    info = data.get("jobPostingInfo") or {}
    title = text_or_none(info.get("title")) or text_or_none((listing or {}).get("title"))
    if not title:
        return None
    host, site = ref.params["host"], ref.params["site"]
    path = (listing or {}).get("externalPath") or ""
    url = text_or_none(info.get("externalUrl")) or f"https://{host}/{site}{path}"
    locations = [
        loc
        for raw in [info.get("location"), *(info.get("additionalLocations") or [])]
        if (loc := text_or_none(raw))
    ]
    req_id = text_or_none(info.get("jobReqId")) or text_or_none(info.get("id")) or path
    country = (info.get("country") or {}).get("descriptor")
    return RawJob(
        source=KIND,
        external_id=f"{ref.key}:{req_id}",
        url=url,
        title=title,
        company_name=text_or_none((data.get("hiringOrganization") or {}).get("name")),
        description=text_or_none(info.get("jobDescription")),
        locations=list(dict.fromkeys(locations)),
        work_mode=text_or_none(info.get("remoteType")),
        employment_type=text_or_none(info.get("timeType")),
        # `startDate` là ngày bắt đầu đăng tin (khớp "Posted N Days Ago").
        posted_at=parse_datetime(info.get("startDate")),
        posted_raw=text_or_none(info.get("postedOn")),
        expires_at=parse_datetime(info.get("endDate"), end_of_day=True),
        expires_raw=text_or_none(info.get("endDate")),
        apply_url=url,
        method=ExtractionMethod.ATS_API,
        extra={k: v for k, v in {"country": country, "job_req_id": req_id}.items() if v},
    )


def listing_only(posting: dict[str, Any], *, ref: AtsRef) -> RawJob:
    """Tin chỉ có ở danh sách (chưa lấy chi tiết vì hết giới hạn trang)."""
    host, site = ref.params["host"], ref.params["site"]
    url = f"https://{host}/{site}{posting['externalPath']}"
    bullets = posting.get("bulletFields") or []
    req_id = text_or_none(bullets[0]) if bullets else posting["externalPath"]
    return RawJob(
        source=KIND,
        external_id=f"{ref.key}:{req_id}",
        url=url,
        title=posting["title"],
        locations=[loc] if (loc := text_or_none(posting.get("locationsText"))) else [],
        posted_raw=text_or_none(posting.get("postedOn")),
        apply_url=url,
        method=ExtractionMethod.ATS_API,
        extra={"detail": "skipped"},
    )


class WorkdaySource(Source):
    kind = KIND

    def __init__(self, ref: AtsRef) -> None:
        super().__init__(ref.key, ref.url)
        self.ref = ref
        host, tenant, site = ref.params["host"], ref.params["tenant"], ref.params["site"]
        self.api = f"https://{host}/wday/cxs/{tenant}/{site}"

    async def _page(
        self, session: SourceSession, offset: int, facets: dict[str, list[str]]
    ) -> dict[str, Any]:
        body = {"appliedFacets": facets, "limit": PAGE_SIZE, "offset": offset, "searchText": ""}
        result = await session.fetch_ok(
            f"{self.api}/jobs",
            method="POST",
            json=body,
            headers=JSON_HEADERS,
            rate=session.limits.ats_rate,
        )
        data = result.json()
        session.count(result, len(data.get("jobPostings") or []))
        return data

    async def _listing(self, session: SourceSession) -> list[dict[str, Any]]:
        facets: dict[str, list[str]] = {}
        first = await self._page(session, 0, facets)
        total = int(first.get("total") or 0)
        capacity = session.limits.max_listing_pages * PAGE_SIZE
        if total > capacity and (vn := vietnam_facets(first.get("facets"))):
            facets = vn
            first = await self._page(session, 0, facets)
            total = int(first.get("total") or 0)
        postings = parse_listing(first)
        # `total` chỉ đáng tin ở trang đầu; dừng khi một trang trả ít hơn PAGE_SIZE.
        page = first
        while len(page.get("jobPostings") or []) == PAGE_SIZE and len(postings) < min(
            total, capacity, session.limits.max_jobs
        ):
            page = await self._page(session, len(postings), facets)
            postings.extend(parse_listing(page))
        return vietnam_first(postings, lambda p: p.get("locationsText"))

    async def _detail(self, session: SourceSession, posting: dict[str, Any]) -> RawJob:
        result = await session.fetch(
            f"{self.api}{posting['externalPath']}",
            headers=JSON_HEADERS,
            rate=session.limits.ats_rate,
        )
        if result.ok:
            try:
                job = parse_detail(result.json(), ref=self.ref, listing=posting)
            except ValueError:
                job = None
            if job is not None:
                session.count(result, 1)
                return job
        return listing_only(posting, ref=self.ref)

    async def jobs(self, session: SourceSession) -> AsyncIterator[RawJob]:
        postings = await self._listing(session)
        limit = session.limits.max_detail_pages
        step = session.limits.detail_concurrency
        detailed = postings[:limit]
        for i in range(0, len(detailed), step):
            batch = detailed[i : i + step]
            for job in await gather_all(self._detail(session, p) for p in batch):
                yield job
        for posting in postings[limit:]:
            yield listing_only(posting, ref=self.ref)


SPEC = AtsSpec(kind=KIND, patterns=PATTERNS, make_ref=make_ref, make_source=WorkdaySource)
