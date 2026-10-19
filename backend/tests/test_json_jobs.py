"""Tin trong JSON của trang careers SPA, trên dữ liệu thật (tests/fixtures/careers/json/).

Mỗi file `.json` là một phản hồi API bắt được khi mở trang bằng trình duyệt: `url`, `method`,
`request_json` của request và `body`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobcrawler.pipeline.dedup import _apply_key  # pyright: ignore[reportPrivateUsage]
from jobcrawler.schemas import ExtractionMethod, RawJob
from jobcrawler.sources.json_jobs import (
    JsonDoc,
    embedded_json,
    extract,
    id_names,
    job_arrays,
    pager_for,
    to_job,
)

from .jobs_fixtures import normalized

FIXTURES = Path(__file__).parent / "fixtures" / "careers" / "json"


def doc(name: str) -> JsonDoc:
    captured = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return JsonDoc(
        url=captured["url"],
        data=captured["body"],
        method=captured["method"],
        request_json=captured["request_json"],
    )


def test_ahamove_jobs_api() -> None:
    api = doc("ahamove_jobs.json")
    [array] = job_arrays(api)
    assert (array.path, len(array.items)) == ("$.items", 7)
    page = "https://careers.ahamove.com/jobs"
    links = {f"{page}/ctv-kinh-doanh-3": "CTV Kinh Doanh"}
    found = extract(api, page_url=page, links=links)
    assert len(found.jobs) == 7
    ctv = found.jobs[0]
    # URL trang tin: link trên trang có đúng slug của phần tử.
    assert (ctv.title, ctv.url) == ("CTV Kinh Doanh", f"{page}/ctv-kinh-doanh-3")
    assert ctv.locations == ["Thành phố Hồ Chí Minh, Đồng Nai"]
    assert ctv.employment_type == "CTV"
    assert ctv.department == "Business Development"
    assert ctv.method is ExtractionMethod.PARSER
    assert len(ctv.description or "") > 500
    assert ctv.requirements
    assert ctv.benefits
    # Không có link: tin dùng URL trang danh sách, không coi URL đó là nơi nộp chung.
    other = found.jobs[1]
    assert other.url == page
    assert other.extra["shared_url"] is True
    assert len({j.external_id for j in found.jobs}) == 7
    pager = pager_for(api, array)
    assert pager is not None
    assert (pager.kind, pager.key, pager.current, pager.size, pager.total) == (
        "page",
        "page",
        1,
        10,
        7,
    )
    assert pager.done(7)


def test_shopee_offset_api() -> None:
    api = doc("shopee_job_list.json")
    [array] = job_arrays(api)
    assert array.path == "$.data.job_list"
    found = extract(api, page_url="https://careers.shopee.vn/jobs", links={})
    assert [j.title for j in found.jobs][:1] == [
        "Key Account Management Associate - Fulfillment Services (HN/HCM) - Shopee"
    ]
    assert all(len(j.description or "") >= 100 for j in found.jobs)
    assert found.jobs[0].locations == []  # chỉ có mã thành phố, không đoán
    # Danh mục nơi làm trang tải kèm: mã → tên.
    names = id_names([doc("shopee_meta.json"), api])
    assert names[("region", "32")] == "Vietnam"
    named = extract(api, page_url="https://careers.shopee.vn/jobs", links={}, names=names)
    assert named.jobs[0].locations[0] in {"Hanoi, Vietnam", "Ho Chi Minh City, Vietnam"}
    # Mã ổn định giữa các lượt.
    again = extract(api, page_url="https://careers.shopee.vn/jobs", links={})
    assert [j.external_id for j in again.jobs] == [j.external_id for j in found.jobs]
    pager = pager_for(api, array)
    assert pager is not None
    assert (pager.kind, pager.key, pager.size, pager.total) == ("offset", "offset", 50, 2645)
    url, body = pager.request(1)
    assert url == "https://ats.workatsea.com/ats/api/v1/user/job/list/?limit=50&offset=50"
    assert body is None
    assert pager.request(2)[0].endswith("offset=100")
    assert not pager.done(100)


def test_ghn_api_title_in_nested_object() -> None:
    api = doc("ghn_search_recruit.json")
    [array] = job_arrays(api)
    found = extract(api, page_url="https://tuyendung.ghn.vn/recruit?tag=recruit", links={})
    job = found.jobs[0]
    assert job.title == "[Đồng Nai - Bình Dương] Nhân viên kinh doanh thị trường (B2C)"
    assert (job.salary_min, job.salary_max) == (10_000_000, 15_000_000)
    assert job.employment_type == "Toàn thời gian"
    assert "Hồ Chí Minh" in job.locations
    assert job.expires_at is not None
    assert job.benefits
    pager = pager_for(api, array)
    assert pager is not None
    assert (pager.kind, pager.current, pager.size, pager.total) == ("page", 1, 6, 22)
    assert "page=2" in pager.request(1)[0]
    assert "pageSize=6" in pager.request(1)[0]


@pytest.mark.parametrize(
    "name",
    [
        "ahamove_job_fields.json",  # danh mục {code, name}
        "ghn_blog_staff.json",  # câu chuyện nhân viên {title, content}
        "ghn_job_titles.json",  # danh sách tên vị trí để lọc
    ],
)
def test_non_job_arrays_are_ignored(name: str) -> None:
    api = doc(name)
    assert job_arrays(api) == []
    assert extract(api, page_url="https://a.vn/", links={}).jobs == []


def test_items_with_url_but_no_description_become_links() -> None:
    api = doc("ghn_footer.json")
    found = extract(api, page_url="https://tuyendung.ghn.vn/recruit", links={})
    assert found.jobs == []
    assert found.links[0] == (
        "https://tuyendung.ghn.vn/recruit/detail/bdscvptkd-1688465121",
        "Chuyên Viên Phát Triển Kinh Doanh",
    )


def test_next_data_jobs_and_html_pager() -> None:
    page = "https://career.vng.com.vn/vi/tim-kiem-viec-lam"
    html = (FIXTURES / "vng_search.html").read_text(encoding="utf-8")
    [data] = embedded_json(html)  # __NEXT_DATA__ cũng là application/json: chỉ đọc một lần
    embedded = JsonDoc(url=page, data=data, embedded=True)
    [array] = job_arrays(embedded)
    assert array.path == "$.props.pageProps.jobs"
    found = extract(embedded, page_url=page, links={})
    assert len(found.jobs) == 10
    assert found.jobs[0].title == "Technical Support Engineer, GreenNode"
    assert found.jobs[0].locations == ["Thành phố Hà Nội"]
    pager = pager_for(embedded, array)
    assert pager is not None
    assert pager.embedded
    assert (pager.current, pager.size, pager.total) == (1, 10, 78)
    assert pager.request(1)[0] == f"{page}?page=2"


def test_pager_in_json_body() -> None:
    api = JsonDoc(
        url="https://hr.a.vn/api/jobs/search",
        method="POST",
        request_json={"filter": {"keyword": ""}, "paging": {"limit": 2, "offset": 0}},
        data={
            "total": 5,
            "jobs": [
                {"title": f"Vị trí {i}", "location": "Hà Nội", "description": "Mô tả. " * 30}
                for i in range(2)
            ],
        },
    )
    [array] = job_arrays(api)
    pager = pager_for(api, array)
    assert pager is not None
    assert pager.in_body
    url, body = pager.request(1)
    assert url == api.url
    assert body == {"filter": {"keyword": ""}, "paging": {"limit": 2, "offset": 2}}


def test_empty_api_dates_are_ignored() -> None:
    item = {
        "title": "Trưởng Bưu Cục",
        "address": "Quận 10",
        "end_time": "0001-01-01T00:00:00Z",  # giá trị rỗng của API, không phải hạn nộp
        "content": "<p>" + "Quản lý bưu cục, điều phối nhân viên giao nhận. " * 5 + "</p>",
    }
    job = to_job(
        item, doc=JsonDoc(url="https://a.vn/api/jobs", data={}), page_url="https://a.vn/", links={}
    )
    assert isinstance(job, RawJob)
    assert job.expires_at is None


def test_jobs_sharing_the_listing_url_have_no_common_apply_key() -> None:
    raw = RawJob(
        source="careers",
        external_id="x",
        url="https://careers.shopee.vn/jobs",
        title="Kế toán",
        description="Mô tả",
        method=ExtractionMethod.PARSER,
        extra={"shared_url": True},
    )
    assert _apply_key(normalized(raw)) is None
    own = raw.model_copy(update={"extra": {}, "url": "https://a.vn/jobs/ke-toan-1"})
    assert _apply_key(normalized(own)) == "https://a.vn/jobs/ke-toan-1"
