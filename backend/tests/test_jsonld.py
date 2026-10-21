from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jobcrawler.common.schemas import ExtractionMethod
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


def test_postings_sharing_the_page_url_keep_their_ids_when_one_is_removed() -> None:
    """Trang danh sách có nhiều JobPosting không có `url`: mã theo `identifier` (khi riêng từng
    tin) hoặc tên vị trí, không theo thứ tự trên trang."""
    page = "https://acme.vn/tuyen-dung"
    nodes = [
        {"@type": "JobPosting", "title": "Kế toán", "identifier": "KT-01"},
        {"@type": "JobPosting", "title": "Thủ kho"},
        {"@type": "JobPosting", "title": "Bảo vệ"},
        {"@type": "JobPosting", "title": "Bảo vệ"},
    ]
    before = {(j.title, j.external_id) for j in jsonld.raw_jobs(_page(*nodes), page)}
    assert len({i for _, i in before}) == 4
    after = {(j.title, j.external_id) for j in jsonld.raw_jobs(_page(*nodes[1:]), page)}
    assert after < before  # bỏ tin đầu: các tin còn lại giữ mã
    # `identifier` chung mọi tin (mã công ty): phân biệt bằng tên vị trí.
    shared = [{**n, "identifier": "ACME"} for n in nodes[:2]]
    assert len({j.external_id for j in jsonld.raw_jobs(_page(*shared), page)}) == 2


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


@pytest.mark.parametrize("value", ["0000-00-00", "2026-02-30", "2026-13-01", "2026-02-30T10:00Z"])
def test_parse_datetime_impossible_dates_are_none(value: str) -> None:
    assert parse_datetime(value) is None
    assert parse_datetime(value, end_of_day=True) is None


def test_parse_datetime_out_of_range_is_none() -> None:
    # Nửa đêm 1/1/0001 giờ Việt Nam đổi sang UTC là trước năm 1: ngoài khoảng của `datetime`.
    assert parse_datetime("0001-01-01") is None
    assert parse_datetime("9999-12-31T23:00:00-05:00") is None


def test_bad_valid_through_does_not_drop_the_page() -> None:
    node = {"@type": "JobPosting", "title": "Kế toán", "validThrough": "0000-00-00"}
    [job] = jsonld.raw_jobs(_page(node), "https://a.vn/tuyen-dung/ke-toan-1")
    assert (job.expires_at, job.expires_raw) == (None, "0000-00-00")


def test_deadline_at_local_midnight_is_end_of_that_day() -> None:
    """JobsGO "Hạn nộp: 01/11/2026" ghi `validThrough` "2026-11-01T00:00:00+0700": còn nhận hồ sơ
    hết ngày 1/11. Ngày đăng thì giữ nguyên nửa đêm."""
    end = datetime(2026, 11, 1, 16, 59, 59, tzinfo=UTC)
    assert parse_datetime("2026-11-01T00:00:00+0700", end_of_day=True) == end
    assert parse_datetime("2026-11-01T00:00:00", end_of_day=True) == end
    assert parse_datetime("2026-11-01T00:00:00+0700") == datetime(2026, 10, 31, 17, 0, tzinfo=UTC)
    # Có giờ cụ thể: giữ nguyên.
    assert parse_datetime("2026-11-01T17:30:00+07:00", end_of_day=True) == datetime(
        2026, 11, 1, 10, 30, tzinfo=UTC
    )


def test_salary_text_values() -> None:
    """Lương dạng chữ (Vieclam24h, TopDev, ITviec) giữ nguyên văn, không thành "VND/MONTH"."""
    for value, raw in [
        ({"currency": "VND", "value": {"value": "Thỏa thuận", "unitText": "MONTH"}}, "Thỏa thuận"),
        ({"currency": "USD", "value": "Negotiable"}, "Negotiable"),
        (
            {
                "currency": "USD",
                "value": {"minValue": 1000, "maxValue": 2000, "value": "1,000 - 2,000 USD"},
            },
            "1,000 - 2,000 USD",
        ),
    ]:
        [job] = jsonld.raw_jobs(
            _page({"@type": "JobPosting", "title": "x", "baseSalary": value}), "https://a.vn/"
        )
        assert job.salary_raw == raw
    [job] = jsonld.raw_jobs(
        _page(
            {
                "@type": "JobPosting",
                "title": "x",
                "baseSalary": {
                    "currency": "VND",
                    "minValue": 8000000,
                    "maxValue": 15000000,
                    "unitText": "MONTH",
                },
            }
        ),
        "https://a.vn/",
    )
    assert (job.salary_min, job.salary_max, job.salary_raw) == (
        8_000_000,
        15_000_000,
        "8000000 - 15000000 VND/MONTH",
    )


def test_education_requirements_object() -> None:
    """`educationRequirements` dạng EducationalOccupationalCredential (CareerViet, Glints, JobsGO)
    → tên trình độ, không phải repr của dict."""
    for value, want in [
        (
            {
                "@type": "EducationalOccupationalCredential",
                "credentialCategory": "bachelor degree",
                "alternateName": "Đại học/Cử nhân",
            },
            "Trình độ: Đại học/Cử nhân",
        ),
        (
            {"@type": "EducationalOccupationalCredential", "credentialCategory": "bachelor degree"},
            "Trình độ: bachelor degree",
        ),
        (
            [
                {"@type": "EducationalOccupationalCredential", "credentialCategory": "high school"},
                "Tốt nghiệp THPT",
            ],
            "Trình độ: high school\nTốt nghiệp THPT",
        ),
        ("Cao đẳng trở lên", "Cao đẳng trở lên"),
        ({"@type": "EducationalOccupationalCredential"}, None),
    ]:
        [job] = jsonld.raw_jobs(
            _page({"@type": "JobPosting", "title": "x", "educationRequirements": value}),
            "https://a.vn/",
        )
        assert job.requirements == want
    # `qualifications` vẫn được ưu tiên.
    [job] = jsonld.raw_jobs(
        _page(
            {
                "@type": "JobPosting",
                "title": "x",
                "qualifications": "<ul><li>SQL</li></ul>",
                "educationRequirements": {"credentialCategory": "bachelor degree"},
            }
        ),
        "https://a.vn/",
    )
    assert job.requirements == "<ul><li>SQL</li></ul>"
    # Trang thật.
    boards = Path(__file__).parent / "fixtures" / "boards"
    html = (boards / "careerviet" / "job_1.html").read_text(encoding="utf-8")
    [job] = jsonld.raw_jobs(html, "https://careerviet.vn/")
    assert job.requirements == "Trình độ: Đại học/Cử nhân"
