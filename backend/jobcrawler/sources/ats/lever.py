"""Lever: Postings API công khai, một request lấy cả nội dung.

`GET https://api.lever.co/v0/postings/{site}?mode=json` (robots.txt: Crawl-delay 1).
Mẫu: tests/fixtures/lever/.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any

from jobcrawler.common.schemas import ExtractionMethod, RawJob
from jobcrawler.sources.ats.ref import AtsRef, AtsSpec
from jobcrawler.sources.base import (
    Source,
    SourceSession,
    parse_datetime,
    text_or_none,
    vietnam_first,
)

KIND = "lever"
API = "https://api.lever.co/v0/postings/{site}?mode=json"

PATTERNS = (
    re.compile(r"api\.lever\.co/v0/postings/(?P<site>[A-Za-z0-9_.-]+)", re.IGNORECASE),
    re.compile(r"(?<![.\w])jobs\.lever\.co/(?P<site>[A-Za-z0-9_.-]+)", re.IGNORECASE),
)


def make_ref(m: re.Match[str]) -> AtsRef | None:
    site = m["site"]
    return AtsRef(
        kind=KIND, key=site.lower(), url=f"https://jobs.lever.co/{site}", params={"site": site}
    )


def _description(item: dict[str, Any]) -> str | None:
    """Mô tả + các danh sách (yêu cầu, trách nhiệm...) + phần bổ sung, theo thứ tự trên trang."""
    parts: list[str] = []
    if desc := text_or_none(item.get("description")):
        parts.append(desc)
    for block in item.get("lists") or []:
        heading = text_or_none(block.get("text"))
        content = text_or_none(block.get("content"))
        if heading:
            parts.append(f"<h3>{heading}</h3>")
        if content:
            parts.append(f"<ul>{content}</ul>")
    if extra := text_or_none(item.get("additional")):
        parts.append(extra)
    return "\n".join(parts) or None


def parse_postings(data: list[dict[str, Any]], site: str) -> list[RawJob]:
    jobs: list[RawJob] = []
    for item in data:
        if not (title := text_or_none(item.get("text"))) or not item.get("id"):
            continue
        categories = item.get("categories") or {}
        locations = [
            loc for x in categories.get("allLocations") or [] if (loc := text_or_none(x))
        ] or ([loc] if (loc := text_or_none(categories.get("location"))) else [])
        url = text_or_none(item.get("hostedUrl")) or f"https://jobs.lever.co/{site}/{item['id']}"
        jobs.append(
            RawJob(
                source=KIND,
                external_id=f"{site.lower()}:{item['id']}",
                url=url,
                title=title,
                description=_description(item),
                locations=locations,
                work_mode=text_or_none(item.get("workplaceType")),
                employment_type=text_or_none(categories.get("commitment")),
                department=text_or_none(categories.get("department") or categories.get("team")),
                posted_at=parse_datetime(item.get("createdAt")),
                apply_url=text_or_none(item.get("applyUrl")) or url,
                method=ExtractionMethod.ATS_API,
                extra={k: v for k, v in {"country": item.get("country")}.items() if v},
            )
        )
    return jobs


class LeverSource(Source):
    kind = KIND

    def __init__(self, ref: AtsRef) -> None:
        super().__init__(ref.key, ref.url)
        self.site = ref.params.get("site", ref.key)

    async def jobs(self, session: SourceSession) -> AsyncIterator[RawJob]:
        result = await session.fetch_ok(API.format(site=self.site), rate=session.limits.ats_rate)
        data = result.json()
        jobs = parse_postings(data if isinstance(data, list) else [], self.site)
        session.count(result, len(jobs))
        for job in vietnam_first(jobs, lambda j: " ".join(j.locations)):
            yield job


SPEC = AtsSpec(kind=KIND, patterns=PATTERNS, make_ref=make_ref, make_source=LeverSource)
