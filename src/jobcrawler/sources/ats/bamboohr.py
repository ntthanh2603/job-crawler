"""BambooHR: JSON mà trang careers `{sub}.bamboohr.com/careers` tự gọi.

- Danh sách: `GET https://{sub}.bamboohr.com/careers/list` → `{"meta": {...}, "result": [...]}`.
- Chi tiết: `GET https://{sub}.bamboohr.com/careers/{id}/detail` → `result.jobOpening`.

Subdomain không tồn tại thì bị chuyển về www.bamboohr.com. Mẫu: tests/fixtures/bamboohr/.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any

from jobcrawler.net.urls import host_of
from jobcrawler.schemas import ExtractionMethod, FetchOutcome, RawJob
from jobcrawler.sources.ats.ref import RESERVED_SUBDOMAINS, AtsRef, AtsSpec
from jobcrawler.sources.base import (
    Source,
    SourceError,
    SourceSession,
    gather_all,
    join_nonempty,
    parse_datetime,
    text_or_none,
    transient_failure,
    vietnam_first,
)

KIND = "bamboohr"

PATTERNS = (re.compile(r"(?<![\w.-])(?P<sub>[a-z0-9][a-z0-9-]*)\.bamboohr\.com", re.IGNORECASE),)


def make_ref(m: re.Match[str]) -> AtsRef | None:
    sub = m["sub"].lower()
    if sub in RESERVED_SUBDOMAINS:
        return None
    return AtsRef(kind=KIND, key=sub, url=f"https://{sub}.bamboohr.com/careers")


def _location(item: dict[str, Any]) -> str | None:
    loc = item.get("location") or {}
    ats = item.get("atsLocation") or {}
    return join_nonempty(
        [
            loc.get("city") or ats.get("city"),
            loc.get("state") or ats.get("state") or ats.get("province"),
            loc.get("addressCountry") or ats.get("country"),
        ]
    )


def parse_list(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [x for x in data.get("result") or [] if x.get("id") and x.get("jobOpeningName")]


def parse_detail(data: dict[str, Any], *, sub: str, job_id: str) -> RawJob | None:
    opening = (data.get("result") or {}).get("jobOpening") or {}
    if not (title := text_or_none(opening.get("jobOpeningName"))):
        return None
    url = text_or_none(opening.get("jobOpeningShareUrl")) or (
        f"https://{sub}.bamboohr.com/careers/{job_id}"
    )
    compensation = opening.get("compensation")
    return RawJob(
        source=KIND,
        external_id=f"{sub}:{job_id}",
        url=url,
        title=title,
        description=text_or_none(opening.get("description")),
        locations=[loc] if (loc := _location(opening)) else [],
        work_mode="remote" if opening.get("isRemote") else None,
        employment_type=text_or_none(opening.get("employmentStatusLabel")),
        department=text_or_none(opening.get("departmentLabel")),
        # "Minimum Experience" của BambooHR là cấp bậc (Entry-level, Manager/Supervisor...).
        level=text_or_none(opening.get("minimumExperience")),
        salary_raw=text_or_none(compensation) if not isinstance(compensation, dict) else None,
        posted_at=parse_datetime(opening.get("datePosted")),
        posted_raw=text_or_none(opening.get("datePosted")),
        apply_url=url,
        method=ExtractionMethod.ATS_API,
        extra={
            k: v
            for k, v in {
                "status": opening.get("jobOpeningStatus"),
                "location_type": opening.get("locationType"),
            }.items()
            if v
        },
    )


class BambooHRSource(Source):
    kind = KIND

    def __init__(self, ref: AtsRef) -> None:
        super().__init__(ref.key, ref.url)
        self.base = f"https://{ref.key}.bamboohr.com/careers"

    async def _detail(self, session: SourceSession, item: dict[str, Any]) -> RawJob | None:
        job_id = str(item["id"])
        result = await session.fetch(f"{self.base}/{job_id}/detail", rate=session.limits.ats_rate)
        if not result.ok:
            if transient_failure(result):
                session.truncate("detail_error")
            return None
        try:
            job = parse_detail(result.json(), sub=self.key, job_id=job_id)
        except ValueError:
            return None
        if job is not None:
            session.count(result, 1)
        return job

    async def jobs(self, session: SourceSession) -> AsyncIterator[RawJob]:
        result = await session.fetch_ok(f"{self.base}/list", rate=session.limits.ats_rate)
        if host_of(result.final_url) != f"{self.key}.bamboohr.com":
            raise SourceError(FetchOutcome.ERROR, f"không có trang careers: {result.final_url}")
        try:
            items = parse_list(result.json())
        except ValueError as exc:
            raise SourceError(FetchOutcome.ERROR, f"không phải JSON: {result.url}") from exc
        session.count(result, len(items))
        items = vietnam_first(items, _location)
        if len(items) > session.limits.max_detail_pages:
            session.truncate("detail_pages")
            items = items[: session.limits.max_detail_pages]
        step = session.limits.detail_concurrency
        for i in range(0, len(items), step):
            for job in await gather_all(self._detail(session, x) for x in items[i : i + step]):
                if job is not None:
                    yield job


SPEC = AtsSpec(kind=KIND, patterns=PATTERNS, make_ref=make_ref, make_source=BambooHRSource)
