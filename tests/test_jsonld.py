from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from jobcrawler.schemas import ExtractionMethod
from jobcrawler.sources import jsonld
from jobcrawler.sources.base import parse_datetime

FIXTURES = Path(__file__).parent / "fixtures" / "careers"
BASE_JOB = (
    "https://careers.base.vn/job/hn-business-development-executive-junior-b2b-sales-1yoe-29675"
)


def _page(*nodes: object) -> str:
    scripts = "".join(
        f'<script type="application/ld+json">{json.dumps(n, ensure_ascii=False)}</script>'
        for n in nodes
    )
    return f"<html><head>{scripts}</head><body></body></html>"


def test_base_vn_job_posting() -> None:
    """Trang chi tiết Base E-Hiring: JSON-LD có xuống dòng thô trong chuỗi (không hợp lệ chặt)."""
    html = (FIXTURES / "base_vn" / "job_29675.html").read_text(encoding="utf-8")
    [job] = jsonld.raw_jobs(html, BASE_JOB)
    assert job.title == "[HN] Business Development Executive (Junior - B2B Sales - 1yoe)"
    assert job.method is ExtractionMethod.JSONLD
    assert job.source == "careers"
    assert job.company_name == "Base inc"
    assert job.url == BASE_JOB
    assert job.locations
    assert "Hà Nội" in job.locations[0]
    assert job.description
    assert "<" in job.description
    assert len(job.description) > 500
    assert job.posted_raw
    assert job.posted_at is not None
    assert job.expires_raw
    assert job.expires_at is not None
    assert job.salary_currency == "VND"
    assert job.salary_max
    assert job.salary_max > (job.salary_min or 0)
    # Cùng URL → cùng external_id giữa các lượt chạy (idempotent).
    assert job.external_id == jsonld.external_id_for_url(BASE_JOB)


def test_graph_item_list_and_escaped_html() -> None:
    html = _page(
        {"@context": "https://schema.org", "@type": "WebPage", "name": "Tuyển dụng"},
        {
            "@context": "https://schema.org",
            "@graph": [
                {"@type": "Organization", "name": "ACME"},
                {
                    "@type": "ItemList",
                    "itemListElement": [
                        {
                            "@type": "ListItem",
                            "item": {
                                "@type": "JobPosting",
                                "title": "Kế toán",
                                "url": "/jobs/ke-toan",
                                "description": "&lt;p&gt;Làm sổ sách&lt;/p&gt;",
                                "hiringOrganization": "ACME JSC",
                                "jobLocation": [
                                    {"@type": "Place", "address": "Hà Nội"},
                                    {"@type": "Place", "address": {"addressLocality": "Đà Nẵng"}},
                                ],
                                "jobLocationType": "TELECOMMUTE",
                                "employmentType": ["FULL_TIME", "CONTRACTOR"],
                                "baseSalary": {
                                    "@type": "MonetaryAmount",
                                    "currency": "vnd",
                                    "value": {"value": "15000000", "unitText": "MONTH"},
                                },
                                "identifier": {"@type": "PropertyValue", "value": "KT-01"},
                                "skills": "Excel, MISA",
                            },
                        },
                        {"@type": ["JobPosting"], "name": "Thủ kho", "url": "/jobs/thu-kho"},
                        {"@type": "JobPosting"},  # không có tiêu đề → bỏ
                    ],
                },
            ],
        },
    )
    jobs = jsonld.raw_jobs(html, "https://acme.vn/tuyen-dung")
    assert [j.title for j in jobs] == ["Kế toán", "Thủ kho"]
    assert len({j.external_id for j in jobs}) == 2
    first = jobs[0]
    assert first.url == "https://acme.vn/jobs/ke-toan"
    assert first.description == "<p>Làm sổ sách</p>"
    assert first.company_name == "ACME JSC"
    assert first.locations == ["Hà Nội", "Đà Nẵng"]
    assert first.work_mode == "TELECOMMUTE"
    assert first.employment_type == "FULL_TIME, CONTRACTOR"
    assert (first.salary_min, first.salary_max, first.salary_currency, first.salary_period) == (
        15_000_000,
        15_000_000,
        "VND",
        "MONTH",
    )
    assert first.salary_raw == "15000000 VND/MONTH"
    assert first.skills == ["Excel", "MISA"]
    assert first.extra["identifier"] == "KT-01"


def test_broken_json_ld_is_ignored() -> None:
    html = '<script type="application/ld+json">{"@type": "JobPosting", "title": </script>'
    assert jsonld.raw_jobs(html, "https://a.vn/") == []
    assert jsonld.raw_jobs("<html>không có json-ld</html>", "https://a.vn/") == []


def test_parse_datetime_formats() -> None:
    assert parse_datetime("2026-10-15T15:24:46-04:00") == datetime(
        2026, 10, 15, 19, 24, 46, tzinfo=UTC
    )
    assert parse_datetime("2026-10-15 09:31:36 UTC") == datetime(
        2026, 10, 15, 9, 31, 36, tzinfo=UTC
    )
    assert parse_datetime("2026-03-12T16:38:15.322Z") == datetime(
        2026, 3, 12, 16, 38, 15, 322000, tzinfo=UTC
    )
    # Chỉ có ngày: đầu ngày (hoặc cuối ngày cho hạn nộp) theo giờ Việt Nam.
    assert parse_datetime("2026-03-31") == datetime(2026, 3, 30, 17, 0, tzinfo=UTC)
    assert parse_datetime("2026-03-31", end_of_day=True) == datetime(
        2026, 3, 31, 16, 59, 59, tzinfo=UTC
    )
    assert parse_datetime("Fri, 24 Jul 2026 09:05:16 +0200") == datetime(
        2026, 7, 24, 7, 5, 16, tzinfo=UTC
    )
    assert parse_datetime(1786469891368) == datetime.fromtimestamp(1786469891.368, UTC)
    for vague in ("Posted 30+ Days Ago", "2 ngày trước", "", None, True):
        assert parse_datetime(vague) is None
