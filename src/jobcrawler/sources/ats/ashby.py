"""Ashby: Posting API công khai, một request lấy cả nội dung và lương.

`GET https://api.ashbyhq.com/posting-api/job-board/{name}?includeCompensation=true`.
Mẫu: tests/fixtures/ashby/.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import quote, unquote

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

KIND = "ashby"
API = "https://api.ashbyhq.com/posting-api/job-board/{name}?includeCompensation=true"
_RESERVED = frozenset({"api", "embed"})

PATTERNS = (
    re.compile(r"api\.ashbyhq\.com/posting-api/job-board/(?P<name>[^/?#\"'\s<>]+)", re.I),
    re.compile(r"jobs\.ashbyhq\.com/(?P<name>[^/?#\"'\s<>]+)", re.IGNORECASE),
)


def make_ref(m: re.Match[str]) -> AtsRef | None:
    name = unquote(m["name"])
    if name.lower() in _RESERVED:
        return None
    return AtsRef(
        kind=KIND,
        key=name.lower(),
        url=f"https://jobs.ashbyhq.com/{quote(name)}",
        params={"name": name},
    )


def _salary(compensation: dict[str, Any] | None) -> dict[str, Any]:
    if not compensation:
        return {}
    fields: dict[str, Any] = {
        "salary_raw": text_or_none(
            compensation.get("scrapeableCompensationSalarySummary")
            or compensation.get("compensationTierSummary")
        )
    }
    for comp in compensation.get("summaryComponents") or []:
        if comp.get("compensationType") == "Salary":
            fields |= {
                "salary_min": comp.get("minValue"),
                "salary_max": comp.get("maxValue"),
                "salary_currency": text_or_none(comp.get("currencyCode")),
                "salary_period": text_or_none(comp.get("interval")),
            }
            break
    return fields


def _secondary(value: Any) -> str | None:
    if isinstance(value, dict):
        return text_or_none(value.get("location"))
    return text_or_none(value)


def parse_job_board(data: dict[str, Any], name: str) -> list[RawJob]:
    jobs: list[RawJob] = []
    for item in data.get("jobs") or []:
        if item.get("isListed") is False:
            continue
        if not (title := text_or_none(item.get("title"))) or not item.get("id"):
            continue
        locations = [
            loc
            for loc in [
                text_or_none(item.get("location")),
                *(_secondary(x) for x in item.get("secondaryLocations") or []),
            ]
            if loc
        ]
        work_mode = text_or_none(item.get("workplaceType")) or (
            "Remote" if item.get("isRemote") else None
        )
        url = text_or_none(item.get("jobUrl")) or f"https://jobs.ashbyhq.com/{quote(name)}"
        jobs.append(
            RawJob(
                source=KIND,
                external_id=f"{name.lower()}:{item['id']}",
                url=url,
                title=title,
                description=text_or_none(item.get("descriptionHtml")),
                locations=list(dict.fromkeys(locations)),
                work_mode=work_mode,
                employment_type=text_or_none(item.get("employmentType")),
                department=join_nonempty([item.get("department"), item.get("team")], " / "),
                posted_at=parse_datetime(item.get("publishedAt")),
                posted_raw=text_or_none(item.get("publishedAt")),
                apply_url=text_or_none(item.get("applyUrl")) or url,
                method=ExtractionMethod.ATS_API,
                **_salary(item.get("compensation")),
            )
        )
    return jobs


class AshbySource(Source):
    kind = KIND

    def __init__(self, ref: AtsRef) -> None:
        super().__init__(ref.key, ref.url)
        self.board = ref.params.get("name", ref.key)

    async def jobs(self, session: SourceSession) -> AsyncIterator[RawJob]:
        result = await session.fetch_ok(
            API.format(name=quote(self.board)), rate=session.limits.ats_rate
        )
        jobs = parse_job_board(result.json(), self.board)
        session.count(result, len(jobs))
        for job in vietnam_first(jobs, lambda j: " ".join(j.locations)):
            yield job


SPEC = AtsSpec(kind=KIND, patterns=PATTERNS, make_ref=make_ref, make_source=AshbySource)
