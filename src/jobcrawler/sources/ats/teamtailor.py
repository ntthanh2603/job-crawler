"""Teamtailor: RSS công khai của career site, một request lấy cả nội dung.

`GET https://{sub}.teamtailor.com/jobs.rss` (career site gắn sẵn
`<link rel="alternate" type="application/rss+xml" href=".../jobs.rss">`; domain riêng của
doanh nghiệp dùng cùng đường dẫn). Mẫu: tests/fixtures/teamtailor/.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator

from jobcrawler.net.urls import normalize_url, site_root
from jobcrawler.schemas import ExtractionMethod, FetchOutcome, RawJob
from jobcrawler.sources.ats.ref import RESERVED_SUBDOMAINS, AtsRef, AtsSpec
from jobcrawler.sources.base import (
    Source,
    SourceError,
    SourceSession,
    join_nonempty,
    parse_datetime,
    text_or_none,
    vietnam_first,
)

KIND = "teamtailor"
TT = "{https://teamtailor.com/locations}"
CDN_MARKER = "teamtailor-cdn.com"
_RSS_LINK = re.compile(
    r"""<link[^>]+type=["']application/rss\+xml["'][^>]*href=["']([^"']+/jobs\.rss)["']""",
    re.IGNORECASE,
)

PATTERNS = (re.compile(r"(?<![\w.-])(?P<sub>[a-z0-9][a-z0-9-]*)\.teamtailor\.com", re.IGNORECASE),)


def make_ref(m: re.Match[str]) -> AtsRef | None:
    sub = m["sub"].lower()
    if sub in RESERVED_SUBDOMAINS:
        return None
    host = f"{sub}.teamtailor.com"
    return AtsRef(
        kind=KIND, key=host, url=f"https://{host}/jobs", params={"rss": f"https://{host}/jobs.rss"}
    )


def ref_from_html(html: str, page_url: str) -> AtsRef | None:
    """Career site Teamtailor trên domain riêng (vd. careers.congty.vn)."""
    if CDN_MARKER not in html or (m := _RSS_LINK.search(html)) is None:
        return None
    rss = normalize_url(m.group(1), page_url)
    if rss is None:
        return None
    root = site_root(rss)
    host = root.split("/")[2]
    return AtsRef(kind=KIND, key=host, url=f"{root}jobs", params={"rss": rss})


def _text(item: ET.Element, tag: str) -> str | None:
    node = item.find(tag)
    return text_or_none(node.text) if node is not None else None


def parse_rss(xml: str | bytes, host: str) -> list[RawJob]:
    root = ET.fromstring(xml)
    jobs: list[RawJob] = []
    for item in root.iter("item"):
        title, link = _text(item, "title"), _text(item, "link")
        if not title or not link:
            continue
        guid = _text(item, "guid") or link
        locations = [
            loc
            for node in item.iter(f"{TT}location")
            if (
                loc := join_nonempty(
                    [
                        _text(node, f"{TT}city") or _text(node, f"{TT}name"),
                        _text(node, f"{TT}country"),
                    ]
                )
            )
        ]
        published = _text(item, "pubDate")
        jobs.append(
            RawJob(
                source=KIND,
                external_id=f"{host}:{guid}",
                url=link,
                title=title,
                description=_text(item, "description"),
                locations=list(dict.fromkeys(locations)),
                work_mode=_text(item, "remoteStatus"),
                department=_text(item, f"{TT}department"),
                posted_at=parse_datetime(published),
                posted_raw=published,
                apply_url=link,
                method=ExtractionMethod.ATS_API,
                extra={k: v for k, v in {"role": _text(item, f"{TT}role")}.items() if v},
            )
        )
    return jobs


class TeamtailorSource(Source):
    kind = KIND

    def __init__(self, ref: AtsRef) -> None:
        super().__init__(ref.key, ref.url)
        self.rss = ref.params.get("rss") or f"https://{ref.key}/jobs.rss"

    async def jobs(self, session: SourceSession) -> AsyncIterator[RawJob]:
        result = await session.fetch_ok(self.rss, rate=session.limits.ats_rate)
        try:
            jobs = parse_rss(result.content, self.key)
        except ET.ParseError as exc:
            raise SourceError(FetchOutcome.ERROR, f"RSS lỗi: {exc}") from exc
        session.count(result, len(jobs))
        for job in vietnam_first(jobs, lambda j: " ".join(j.locations)):
            yield job


SPEC = AtsSpec(kind=KIND, patterns=PATTERNS, make_ref=make_ref, make_source=TeamtailorSource)
