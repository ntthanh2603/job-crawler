"""Adapter ATS và nhận diện ATS từ URL hoặc HTML của trang careers."""

from __future__ import annotations

import re

from jobcrawler.sources.ats import (
    ashby,
    bamboohr,
    greenhouse,
    lever,
    recruitee,
    teamtailor,
    workable,
    workday,
)
from jobcrawler.sources.ats.ref import AtsRef, AtsSpec
from jobcrawler.sources.base import Source

# SmartRecruiters: robots.txt của api.smartrecruiters.com cấm mọi bot (trừ LinkedInBot) và
# careers.smartrecruiters.com trả thử thách Cloudflare → chỉ nhận diện, không thu thập.
SMARTRECRUITERS = AtsSpec(
    kind="smartrecruiters",
    patterns=(
        re.compile(
            r"(?:careers|jobs)\.smartrecruiters\.com/(?P<company>[A-Za-z0-9_-]+)", re.IGNORECASE
        ),
    ),
    make_ref=lambda m: AtsRef(
        kind="smartrecruiters",
        key=m["company"].lower(),
        url=f"https://careers.smartrecruiters.com/{m['company']}",
    ),
    make_source=None,
    note="robots.txt cấm API; trang careers bị Cloudflare chặn",
)

SPECS: tuple[AtsSpec, ...] = (
    greenhouse.SPEC,
    lever.SPEC,
    ashby.SPEC,
    workable.SPEC,
    workday.SPEC,
    bamboohr.SPEC,
    recruitee.SPEC,
    teamtailor.SPEC,
    SMARTRECRUITERS,
)
_BY_KIND = {spec.kind: spec for spec in SPECS}

# URL trong thuộc tính, script inline hoặc JSON đã escape (`https:\/\/jobs.lever.co\/x`).
_URL = re.compile(r"""(?:https?:)?//[^\s"'<>()\\]+""", re.IGNORECASE)
_ATS_HOSTS = re.compile(
    r"greenhouse\.io|lever\.co|ashbyhq\.com|workable\.com|myworkdayjobs\.com|bamboohr\.com|"
    r"recruitee\.com|teamtailor\.com|smartrecruiters\.com",
    re.IGNORECASE,
)


def match_url(url: str) -> AtsRef | None:
    """URL thuộc một bảng tin ATS đã biết."""
    if not _ATS_HOSTS.search(url):
        return None
    for spec in SPECS:
        if (ref := spec.match(url)) is not None:
            return ref
    return None


def detect(html: str, page_url: str) -> list[AtsRef]:
    """Mọi bảng tin ATS được trang nhắc tới (link, iframe, script nhúng), theo thứ tự xuất hiện."""
    found: dict[str, AtsRef] = {}
    if (ref := match_url(page_url)) is not None:
        found[ref.name] = ref
    text = html.replace("\\/", "/")
    for m in _URL.finditer(text):
        if (ref := match_url(m.group(0))) is not None:
            found.setdefault(ref.name, ref)
    if (ref := teamtailor.ref_from_html(html, page_url)) is not None:
        found.setdefault(ref.name, ref)
    return list(found.values())


def supported(ref: AtsRef) -> bool:
    spec = _BY_KIND.get(ref.kind)
    return spec is not None and spec.make_source is not None


def unsupported_reason(ref: AtsRef) -> str | None:
    spec = _BY_KIND.get(ref.kind)
    if spec is None:
        return "ATS chưa biết"
    return spec.note if spec.make_source is None else None


def source_for(ref: AtsRef) -> Source | None:
    spec = _BY_KIND.get(ref.kind)
    if spec is None or spec.make_source is None:
        return None
    return spec.make_source(ref)


__all__ = ["SPECS", "AtsRef", "AtsSpec", "detect", "match_url", "source_for", "supported"]
