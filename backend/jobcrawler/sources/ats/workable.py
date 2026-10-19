"""Workable: widget API công khai, một request lấy cả nội dung (`details=true`).

`GET https://apply.workable.com/api/v1/widget/accounts/{account}?details=true`.
Mẫu: tests/fixtures/workable/.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any

from jobcrawler.schemas import ExtractionMethod, RawJob
from jobcrawler.sources.ats.ref import RESERVED_SUBDOMAINS, AtsRef, AtsSpec
from jobcrawler.sources.base import (
    Source,
    SourceSession,
    join_nonempty,
    parse_datetime,
    text_or_none,
    vietnam_first,
)

KIND = "workable"
API = "https://apply.workable.com/api/v1/widget/accounts/{account}?details=true"
# Đường dẫn của chính apply.workable.com, không phải account.
_RESERVED = frozenset({"api", "j", "careers", "static", "assets", "embed"})

PATTERNS = (
    re.compile(r"apply\.workable\.com/api/v\d/(?:widget/)?accounts/(?P<account>[\w-]+)", re.I),
    re.compile(r"apply\.workable\.com/(?P<account>[\w-]+)", re.IGNORECASE),
    # Kiểu cũ: https://{account}.workable.com/
    re.compile(r"(?<![\w.-])(?P<sub>[a-z0-9][a-z0-9-]*)\.workable\.com", re.IGNORECASE),
)


def make_ref(m: re.Match[str]) -> AtsRef | None:
    groups = m.groupdict()
    account = groups.get("account") or groups.get("sub") or ""
    if account.lower() in _RESERVED | RESERVED_SUBDOMAINS | {"apply"}:
        return None
    return AtsRef(
        kind=KIND,
        key=account.lower(),
        url=f"https://apply.workable.com/{account}/",
        params={"account": account},
    )


def _location(loc: dict[str, Any]) -> str | None:
    return join_nonempty([loc.get("city"), loc.get("region"), loc.get("country")])


def parse_widget(data: dict[str, Any], account: str) -> list[RawJob]:
    company = text_or_none(data.get("name"))
    jobs: list[RawJob] = []
    for item in data.get("jobs") or []:
        if not (title := text_or_none(item.get("title"))) or not item.get("shortcode"):
            continue
        locations = [
            loc
            for x in item.get("locations") or []
            if not x.get("hidden") and (loc := _location(x))
        ] or ([loc] if (loc := _location(item)) else [])
        url = text_or_none(item.get("url")) or f"https://apply.workable.com/j/{item['shortcode']}"
        published = item.get("published_on") or item.get("created_at")
        jobs.append(
            RawJob(
                source=KIND,
                external_id=f"{account.lower()}:{item['shortcode']}",
                url=url,
                title=title,
                company_name=company,
                description=text_or_none(item.get("description")),
                locations=list(dict.fromkeys(locations)),
                work_mode="remote" if item.get("telecommuting") else None,
                employment_type=text_or_none(item.get("employment_type")),
                department=text_or_none(item.get("department")),
                experience_raw=text_or_none(item.get("experience")),
                posted_at=parse_datetime(published),
                posted_raw=text_or_none(published),
                apply_url=text_or_none(item.get("application_url")) or url,
                method=ExtractionMethod.ATS_API,
                extra={
                    k: v
                    for k, v in {
                        "function": item.get("function"),
                        "industry": item.get("industry"),
                        "education": item.get("education"),
                        "code": item.get("code"),
                    }.items()
                    if v
                },
            )
        )
    return jobs


class WorkableSource(Source):
    kind = KIND

    def __init__(self, ref: AtsRef) -> None:
        super().__init__(ref.key, ref.url)
        self.account = ref.params.get("account", ref.key)

    async def jobs(self, session: SourceSession) -> AsyncIterator[RawJob]:
        result = await session.fetch_ok(
            API.format(account=self.account), rate=session.limits.ats_rate
        )
        jobs = parse_widget(result.json(), self.account)
        session.count(result, len(jobs))
        for job in vietnam_first(jobs, lambda j: " ".join(j.locations)):
            yield job


SPEC = AtsSpec(kind=KIND, patterns=PATTERNS, make_ref=make_ref, make_source=WorkableSource)
