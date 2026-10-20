"""Recruitee: Careers Site API công khai, một request lấy cả nội dung.

`GET https://{sub}.recruitee.com/api/offers/` → `{"offers": [...]}`.
Mẫu: tests/fixtures/recruitee/.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any

from jobcrawler.common.schemas import ExtractionMethod, RawJob
from jobcrawler.sources.ats.ref import RESERVED_SUBDOMAINS, AtsRef, AtsSpec
from jobcrawler.sources.base import (
    Source,
    SourceSession,
    join_nonempty,
    parse_datetime,
    text_or_none,
    vietnam_first,
)

KIND = "recruitee"

PATTERNS = (re.compile(r"(?<![\w.-])(?P<sub>[a-z0-9][a-z0-9-]*)\.recruitee\.com", re.IGNORECASE),)


def make_ref(m: re.Match[str]) -> AtsRef | None:
    sub = m["sub"].lower()
    if sub in RESERVED_SUBDOMAINS:
        return None
    return AtsRef(kind=KIND, key=sub, url=f"https://{sub}.recruitee.com/")


def _work_mode(item: dict[str, Any]) -> str | None:
    if item.get("remote"):
        return "remote"
    if item.get("hybrid"):
        return "hybrid"
    if item.get("on_site"):
        return "onsite"
    return None


def _locations(item: dict[str, Any]) -> list[str]:
    found = [
        loc
        for x in item.get("locations") or []
        if (loc := join_nonempty([x.get("city"), x.get("state"), x.get("country")]))
    ]
    if not found and (loc := text_or_none(item.get("location"))):
        found = [loc]
    return list(dict.fromkeys(found))


def _salary(salary: dict[str, Any] | None) -> dict[str, Any]:
    if not salary or not any(salary.get(k) for k in ("min", "max")):
        return {}
    return {
        "salary_min": salary.get("min"),
        "salary_max": salary.get("max"),
        "salary_currency": text_or_none(salary.get("currency")),
        "salary_period": text_or_none(salary.get("period")),
    }


def parse_offers(data: dict[str, Any], sub: str) -> list[RawJob]:
    jobs: list[RawJob] = []
    for item in data.get("offers") or []:
        if item.get("status") not in (None, "published"):
            continue
        if not (title := text_or_none(item.get("title"))) or item.get("id") is None:
            continue
        url = (
            text_or_none(item.get("careers_url"))
            or f"https://{sub}.recruitee.com/o/{item.get('slug')}"
        )
        jobs.append(
            RawJob(
                source=KIND,
                external_id=f"{sub}:{item['id']}",
                url=url,
                title=title,
                company_name=text_or_none(item.get("company_name")),
                description=text_or_none(item.get("description")),
                requirements=text_or_none(item.get("requirements")),
                locations=_locations(item),
                work_mode=_work_mode(item),
                employment_type=text_or_none(item.get("employment_type_code")),
                department=text_or_none(item.get("department")),
                level=text_or_none(item.get("experience_code")),
                posted_at=parse_datetime(item.get("published_at")),
                posted_raw=text_or_none(item.get("published_at")),
                expires_at=parse_datetime(item.get("close_at"), end_of_day=True),
                expires_raw=text_or_none(item.get("close_at")),
                apply_url=text_or_none(item.get("careers_apply_url")) or url,
                method=ExtractionMethod.ATS_API,
                extra={
                    k: v
                    for k, v in {
                        "education": item.get("education_code"),
                        "category": item.get("category_code"),
                        "tags": item.get("tags"),
                    }.items()
                    if v
                },
                **_salary(item.get("salary")),
            )
        )
    return jobs


class RecruiteeSource(Source):
    kind = KIND

    async def jobs(self, session: SourceSession) -> AsyncIterator[RawJob]:
        result = await session.fetch_ok(
            f"https://{self.key}.recruitee.com/api/offers/", rate=session.limits.ats_rate
        )
        jobs = parse_offers(result.json(), self.key)
        session.count(result, len(jobs))
        for job in vietnam_first(jobs, lambda j: " ".join(j.locations)):
            yield job


SPEC = AtsSpec(
    kind=KIND,
    patterns=PATTERNS,
    make_ref=make_ref,
    make_source=lambda ref: RecruiteeSource(ref.key, ref.url),
)
