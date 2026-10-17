"""Bóc `schema.org/JobPosting` từ JSON-LD nhúng trong trang (extruct)."""

from __future__ import annotations

import hashlib
import html as htmllib
from typing import Any

from extruct.jsonld import JsonLdExtractor

from jobcrawler.logging import get_logger
from jobcrawler.net.urls import normalize_url
from jobcrawler.schemas import ExtractionMethod, RawJob
from jobcrawler.sources.base import join_nonempty, parse_datetime, text_or_none

log = get_logger(__name__)

_EXTRACTOR = JsonLdExtractor()


def _types(node: dict[str, Any]) -> set[str]:
    value = node.get("@type")
    values = value if isinstance(value, list) else [value]
    return {str(v).rsplit("/", 1)[-1].lower() for v in values if v}


def _walk(node: Any) -> list[dict[str, Any]]:
    """Mọi object trong cây JSON-LD (gồm `@graph`, `ItemList.itemListElement`, mảng lồng)."""
    found: list[dict[str, Any]] = []
    stack = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(reversed(item))
        elif isinstance(item, dict):
            found.append(item)
            for key in ("@graph", "itemListElement", "item", "mainEntity"):
                if key in item:
                    stack.append(item[key])
    return found


def job_postings(html: str, url: str) -> list[dict[str, Any]]:
    """Các object JobPosting trên trang, theo thứ tự xuất hiện."""
    if "ld+json" not in html:
        return []
    try:
        data = _EXTRACTOR.extract(html, base_url=url)
    except Exception as exc:  # JSON-LD hỏng không được làm hỏng cả nguồn
        log.debug("jsonld.error", url=url, error=str(exc))
        return []
    return [node for node in _walk(data) if "jobposting" in _types(node)]


_ORG_TYPES = frozenset(
    {
        "corporation",
        "localbusiness",
        "onlinebusiness",
        "onlinestore",
        "store",
        "bankorcreditunion",
        "financialservice",
        "insuranceagency",
        "airline",
        "professionalservice",
    }
)


def organizations(html: str, url: str) -> list[dict[str, Any]]:
    """Các object Organization (và loại con: Corporation, LocalBusiness...) trên trang."""
    if "ld+json" not in html:
        return []
    try:
        data = _EXTRACTOR.extract(html, base_url=url)
    except Exception as exc:
        log.debug("jsonld.error", url=url, error=str(exc))
        return []
    return [
        node
        for node in _walk(data)
        if any(t.endswith("organization") or t in _ORG_TYPES for t in _types(node))
    ]


def organization_facts(node: dict[str, Any]) -> dict[str, str]:
    """Các trường dùng cho KYC của một Organization, dạng chuỗi gọn."""
    alternate = node.get("alternateName")
    facts = {
        "name": _name(node.get("name")),
        "legalName": _name(node.get("legalName")),
        "alternateName": ", ".join(_string_list(alternate)) if alternate else None,
        "taxID": text_or_none(node.get("taxID")),
        "vatID": text_or_none(node.get("vatID")),
        "address": _address(node) if node.get("address") else None,
        "url": text_or_none(node.get("url")),
    }
    return {k: v for k, v in facts.items() if v}


def _name(value: Any) -> str | None:
    if isinstance(value, dict):
        return text_or_none(value.get("name") or value.get("legalName"))
    if isinstance(value, list) and value:
        return _name(value[0])
    return text_or_none(value)


def _html_text(value: Any) -> str | None:
    """Chuỗi HTML; nhiều site escape HTML hai lần (`&lt;p&gt;`)."""
    if isinstance(value, list):
        value = "\n".join(str(v) for v in value if v)
    text = text_or_none(value)
    if text and "<" not in text and "&lt;" in text:
        text = htmllib.unescape(text)
    return text


def _address(place: Any) -> str | None:
    if isinstance(place, str):
        return text_or_none(place)
    if not isinstance(place, dict):
        return None
    address = place.get("address", place)
    if isinstance(address, list):
        address = address[0] if address else None
    if isinstance(address, str):
        return text_or_none(address)
    if not isinstance(address, dict):
        return _name(place)
    country = address.get("addressCountry")
    return join_nonempty(
        [
            address.get("streetAddress"),
            address.get("addressLocality"),
            address.get("addressRegion"),
            _name(country) if isinstance(country, dict) else country,
        ]
    ) or _name(place)


def _locations(value: Any) -> list[str]:
    places = value if isinstance(value, list) else [value]
    seen: dict[str, None] = {}
    for place in places:
        if (text := _address(place)) is not None:
            seen.setdefault(text, None)
    return list(seen)


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.replace(",", "").strip())
        except ValueError:
            return None
    return None


def _fmt(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:g}"


def _salary(value: Any) -> dict[str, Any]:
    """`baseSalary` (MonetaryAmount) → các trường salary_* của RawJob."""
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, int | float | str) and not isinstance(value, bool):
        return {"salary_raw": str(value)}
    if not isinstance(value, dict):
        return {}
    currency = text_or_none(value.get("currency"))
    amount = value.get("value")
    unit: str | None = None
    low = high = None
    # Chữ thay cho số ("Thỏa thuận", "Negotiable", "1,000 - 2,000 USD"): giữ nguyên văn.
    text: str | None = None
    if isinstance(amount, dict):
        unit = text_or_none(amount.get("unitText"))
        low = _number(amount.get("minValue"))
        high = _number(amount.get("maxValue"))
        inner = amount.get("value")
        if low is None and high is None:
            low = high = _number(inner)
        if isinstance(inner, str) and _number(inner) is None:
            text = text_or_none(inner)
    else:
        low = high = _number(amount)
        if isinstance(amount, str) and low is None:
            text = text_or_none(amount)
    if low is None and high is None:
        # MonetaryAmount phẳng: {"currency": "VND", "minValue": 8000000, "maxValue": ...}.
        low, high = _number(value.get("minValue")), _number(value.get("maxValue"))
    unit = unit or text_or_none(value.get("unitText"))
    if low is None and high is None and currency is None and text is None:
        return {}
    span = " - ".join(_fmt(v) for v in dict.fromkeys(x for x in (low, high) if x is not None))
    raw = join_nonempty([span, currency.upper() if currency else None], " ") if span else None
    return {
        "salary_min": low,
        "salary_max": high,
        "salary_currency": currency.upper() if currency else None,
        "salary_period": unit,
        "salary_raw": text or (f"{raw}/{unit}" if raw and unit else raw),
    }


def _requirements(value: Any) -> str | None:
    """`qualifications` / `educationRequirements`: chuỗi HTML, hoặc object
    EducationalOccupationalCredential → "Trình độ: <alternateName | credentialCategory>"."""
    if isinstance(value, list):
        return _html_text([text for v in value if (text := _requirements(v))])
    if isinstance(value, dict):
        level = _name(value.get("alternateName")) or _name(value.get("credentialCategory"))
        return f"Trình độ: {level}" if level else text_or_none(value.get("description"))
    return _html_text(value)


def _experience(value: Any) -> str | None:
    if isinstance(value, dict):
        months = value.get("monthsOfExperience")
        if months is not None:
            return f"{months} months"
        return text_or_none(value.get("description") or value.get("name"))
    return _html_text(value)


def _identifier(value: Any) -> str | None:
    if isinstance(value, dict):
        return text_or_none(value.get("value") or value.get("@id"))
    if isinstance(value, list) and value:
        return _identifier(value[0])
    return text_or_none(value)


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [s.strip() for s in value.split(",") if s.strip()]
    if isinstance(value, list):
        return [t for v in value if (t := _name(v))]
    return []


def external_id_for_url(url: str, discriminator: str = "") -> str:
    """Định danh ổn định cho tin không có id riêng: hash của URL đã chuẩn hoá (+ phần phân biệt
    khi nhiều tin chung một URL, vd. thứ tự trên trang hoặc tên vị trí)."""
    key = normalize_url(url) or url
    if discriminator:
        key = f"{key}|{discriminator}"
    return hashlib.sha1(key.encode()).hexdigest()[:20]


def to_raw_job(
    node: dict[str, Any], *, page_url: str, source: str = "careers", index: int = 0
) -> RawJob | None:
    """Một object JobPosting → RawJob. Thiếu tiêu đề thì bỏ."""
    title = _name(node.get("title")) or _name(node.get("name"))
    if not title:
        return None
    url = normalize_url(str(node.get("url") or ""), page_url) or page_url
    work_mode = text_or_none(node.get("jobLocationType"))
    employment = node.get("employmentType")
    if isinstance(employment, list):
        employment = ", ".join(str(e) for e in employment if e)
    identifier = _identifier(node.get("identifier"))
    # Nhiều JobPosting trên cùng một trang (trang danh sách) có chung URL: thêm thứ tự.
    discriminator = str(index) if index and url == page_url else ""
    return RawJob(
        source=source,
        external_id=external_id_for_url(url, discriminator),
        url=url,
        title=title,
        company_name=_name(node.get("hiringOrganization")),
        description=_html_text(node.get("description")),
        requirements=_requirements(
            node.get("qualifications") or node.get("educationRequirements") or None
        ),
        benefits=_html_text(node.get("jobBenefits")),
        locations=_locations(node.get("jobLocation")),
        work_mode=work_mode,
        employment_type=text_or_none(employment),
        experience_raw=_experience(node.get("experienceRequirements")),
        skills=_string_list(node.get("skills")),
        posted_at=parse_datetime(node.get("datePosted")),
        posted_raw=text_or_none(node.get("datePosted")),
        expires_at=parse_datetime(node.get("validThrough"), end_of_day=True),
        expires_raw=text_or_none(node.get("validThrough")),
        method=ExtractionMethod.JSONLD,
        extra={
            k: v
            for k, v in {
                "identifier": identifier,
                "occupational_category": _name(node.get("occupationalCategory")),
                "industry": _name(node.get("industry")),
            }.items()
            if v
        },
        **_salary(node.get("baseSalary") or node.get("estimatedSalary")),
    )


def raw_jobs(html: str, url: str, *, source: str = "careers") -> list[RawJob]:
    jobs: list[RawJob] = []
    for i, node in enumerate(job_postings(html, url)):
        if (job := to_raw_job(node, page_url=url, source=source, index=i)) is not None:
            jobs.append(job)
    return jobs
