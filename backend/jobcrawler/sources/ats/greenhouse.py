"""Greenhouse: Job Board API công khai, một request lấy cả nội dung.

`GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`
(robots.txt chỉ cấm `/embed/`). Mẫu: tests/fixtures/greenhouse/.
"""

from __future__ import annotations

import html
import re
from collections.abc import AsyncIterator
from typing import Any

from jobcrawler.schemas import ExtractionMethod, RawJob
from jobcrawler.sources.ats.ref import AtsRef, AtsSpec
from jobcrawler.sources.base import (
    Source,
    SourceSession,
    join_nonempty,
    parse_datetime,
    text_or_none,
    vietnam_first,
)

KIND = "greenhouse"
API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
_RESERVED = frozenset({"embed", "v1", "jobs"})

PATTERNS = (
    re.compile(
        r"(?:boards|job-boards)\.greenhouse\.io/embed/job_(?:board|app)(?:/js)?\?(?:[^\"'\s<>]*?&)?"
        r"for=(?P<token>[A-Za-z0-9_-]+)",
        re.IGNORECASE,
    ),
    re.compile(r"boards-api\.greenhouse\.io/v1/boards/(?P<token>[A-Za-z0-9_-]+)", re.IGNORECASE),
    re.compile(r"(?:boards|job-boards)\.greenhouse\.io/(?P<token>[A-Za-z0-9_-]+)", re.IGNORECASE),
)


def make_ref(m: re.Match[str]) -> AtsRef | None:
    token = m["token"]
    if token.lower() in _RESERVED:
        return None
    return AtsRef(
        kind=KIND,
        key=token.lower(),
        url=f"https://job-boards.greenhouse.io/{token}",
        params={"token": token},
    )


def parse_jobs(data: dict[str, Any], token: str) -> list[RawJob]:
    jobs: list[RawJob] = []
    for item in data.get("jobs") or []:
        if not (title := text_or_none(item.get("title"))) or item.get("id") is None:
            continue
        url = text_or_none(item.get("absolute_url")) or f"https://job-boards.greenhouse.io/{token}"
        content = item.get("content")
        location = (item.get("location") or {}).get("name")
        jobs.append(
            RawJob(
                source=KIND,
                external_id=f"{token.lower()}:{item['id']}",
                url=url,
                title=title,
                company_name=text_or_none(item.get("company_name")),
                # API trả HTML đã escape (`&lt;p&gt;`).
                description=html.unescape(content) if isinstance(content, str) else None,
                # Nhiều địa điểm được nối bằng "; " ("Remote, Canada; Remote, United States").
                locations=[x for part in str(location or "").split(";") if (x := part.strip())],
                department=join_nonempty(d.get("name") for d in item.get("departments") or []),
                posted_at=parse_datetime(item.get("first_published")),
                posted_raw=text_or_none(item.get("first_published")),
                expires_at=parse_datetime(item.get("application_deadline"), end_of_day=True),
                expires_raw=text_or_none(item.get("application_deadline")),
                apply_url=url,
                method=ExtractionMethod.ATS_API,
                extra={
                    k: v
                    for k, v in {
                        "updated_at": item.get("updated_at"),
                        "requisition_id": item.get("requisition_id"),
                        "offices": [o.get("name") for o in item.get("offices") or []],
                    }.items()
                    if v
                },
            )
        )
    return jobs


class GreenhouseSource(Source):
    kind = KIND

    def __init__(self, ref: AtsRef) -> None:
        super().__init__(ref.key, ref.url)
        self.token = ref.params.get("token", ref.key)

    async def jobs(self, session: SourceSession) -> AsyncIterator[RawJob]:
        result = await session.fetch_ok(API.format(token=self.token), rate=session.limits.ats_rate)
        jobs = parse_jobs(result.json(), self.token)
        session.count(result, len(jobs))
        for job in vietnam_first(jobs, lambda j: " ".join(j.locations)):
            yield job


SPEC = AtsSpec(kind=KIND, patterns=PATTERNS, make_ref=make_ref, make_source=GreenhouseSource)
