"""Bộ khung trang careers SPA (sources/careers.py + sources/plan.py): lượt build ghi API bắt được,
lượt poll chỉ gọi API khi API có đủ mọi tin của trang, không thì mở lại trang."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from jobcrawler.common.schemas import RawJob
from jobcrawler.modules.crawl.crawl import suspicious_drop
from jobcrawler.services.fakes import FakeChatClient, FakeFetcher
from jobcrawler.services.llm import LLMClient
from jobcrawler.services.net.http import CapturedJson
from jobcrawler.sources.base import KnownJobs, SourceSession
from jobcrawler.sources.careers import CareersSource
from jobcrawler.sources.plan import PlanSpec

SITE = "https://careers.acme.vn"
LISTING = f"{SITE}/jobs"
API = "https://api.acme.vn/jobs?page=1&pageSize=10"
# Pager đặt tham số trang xuống cuối khi dựng lại request.
API_AGAIN = "https://api.acme.vn/jobs?pageSize=10&page=1"
JD = (
    "Mô tả công việc: hạch toán, lập báo cáo tài chính hằng tháng, đối chiếu công nợ với khách "
    "hàng và nhà cung cấp. Yêu cầu: tốt nghiệp chuyên ngành kế toán, ít nhất hai năm kinh nghiệm. "
    "Quyền lợi: lương tháng 13, bảo hiểm đầy đủ, du lịch hằng năm."
)
SHELL = (
    '<html><head></head><body><div id="root"></div><script src="/app.js"></script></body></html>'
)


def job_url(n: int) -> str:
    return f"{SITE}/jobs/ke-toan-tong-hop-{n}"


def api_body(ids: list[int]) -> bytes:
    items = [
        {
            "id": n,
            "title": f"Kế toán tổng hợp {n}",
            "location": "Hà Nội",
            "salary": "Thoả thuận",
            "deadline": "2027-12-31",
            "url": job_url(n),
        }
        for n in ids
    ]
    return json.dumps({"data": {"items": items, "total": len(ids)}}).encode()


def rendered_dom(ids: list[int]) -> str:
    links = "".join(f'<li><a href="{job_url(n)}">Kế toán tổng hợp {n}</a></li>' for n in ids)
    return f"<html><body><h1>Tuyển dụng</h1><ul>{links}</ul></body></html>"


def detail(n: int) -> str:
    posting = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": f"Kế toán tổng hợp {n}",
        "description": f"<p>{JD}</p>",
        "hiringOrganization": {"@type": "Organization", "name": "Acme"},
    }
    return (
        f'<html><head><script type="application/ld+json">{json.dumps(posting)}</script></head>'
        f"<body><h1>Kế toán tổng hợp {n}</h1><p>{JD}</p></body></html>"
    )


def set_api(fetcher: FakeFetcher, ids: list[int]) -> None:
    for url in (API, API_AGAIN):
        fetcher.add(url, api_body(ids), content_type="application/json")
    for n in ids:
        fetcher.add(job_url(n), detail(n))


def site(dom_ids: list[int], api_ids: list[int]) -> FakeFetcher:
    fetcher = FakeFetcher()
    fetcher.add(LISTING, SHELL)
    for n in dom_ids:
        fetcher.add(job_url(n), detail(n))
    fetcher.add_rendered(
        LISTING,
        rendered_dom(dom_ids),
        captured=[CapturedJson(url=API, method="GET", status=200, body=api_body(api_ids))],
    )
    set_api(fetcher, api_ids)
    return fetcher


def session(fetcher: FakeFetcher, **kw: Any) -> SourceSession:
    return SourceSession(
        "careers:careers.acme.vn", fetcher, LLMClient(FakeChatClient(["{}"]), model=None), **kw
    )


async def build(fetcher: FakeFetcher) -> tuple[PlanSpec, list[RawJob]]:
    s = session(fetcher)
    jobs = [job async for job in CareersSource(LISTING).jobs(s)]
    return s.plan, jobs


def known(jobs: list[RawJob]) -> KnownJobs:
    return KnownJobs.of_refs((j.source, j.external_id, j.url) for j in jobs)


async def poll(fetcher: FakeFetcher, spec: PlanSpec, jobs: list[RawJob]) -> SourceSession:
    s = session(fetcher, mode="poll", plan=spec, known=known(jobs))
    s.polled = [job async for job in CareersSource(LISTING).jobs(s)]  # pyright: ignore[reportAttributeAccessIssue]
    return s


async def test_api_covering_every_job_replaces_the_page() -> None:
    fetcher = site(dom_ids=[1, 2], api_ids=[1, 2])
    spec, jobs = await build(fetcher)
    assert [a.url for a in spec.apis] == [API]
    assert spec.rendered == [LISTING]
    assert spec.api_only == [LISTING]
    fetcher.calls.clear()
    s = await poll(fetcher, spec, jobs)
    assert [url for _, url, _ in fetcher.calls] == [API_AGAIN]  # không mở trang, không render
    assert {j.external_id for j in s.polled} == {j.external_id for j in jobs}  # pyright: ignore[reportAttributeAccessIssue]
    assert s.broken is None


async def test_page_with_jobs_outside_the_api_is_rendered_again() -> None:
    # Trang có 3 tin, API bắt được chỉ trả 2 (vd. khối "tin nổi bật" lấy từ nguồn khác).
    fetcher = site(dom_ids=[1, 2, 3], api_ids=[1, 2])
    spec, jobs = await build(fetcher)
    assert spec.api_only == []
    assert len(jobs) == 3
    fetcher.calls.clear()
    s = await poll(fetcher, spec, jobs)
    kinds = [(kind, url) for kind, url, _ in fetcher.calls]
    assert ("render", LISTING) in kinds
    assert {j.url for j in s.polled} == {job_url(1), job_url(2), job_url(3)}  # pyright: ignore[reportAttributeAccessIssue]


async def test_new_job_in_api_is_the_only_detail_opened() -> None:
    fetcher = site(dom_ids=[1, 2], api_ids=[1, 2])
    spec, jobs = await build(fetcher)
    set_api(fetcher, [1, 2, 3])
    fetcher.calls.clear()
    s = await poll(fetcher, spec, jobs)
    opened = [url for _, url, _ in fetcher.calls]
    assert opened == [API_AGAIN, job_url(3)]  # chỉ mở trang của tin mới
    assert job_url(1) not in opened
    assert job_url(2) not in opened
    assert len(s.polled) == 3  # pyright: ignore[reportAttributeAccessIssue]


def test_suspicious_drop() -> None:
    assert not suspicious_drop(4, 3)  # nguồn nhỏ: đóng thẳng
    assert not suspicious_drop(31, 2)
    assert suspicious_drop(31, 10)  # GHN: 10/31 tin "biến mất" trong một lượt poll
    assert suspicious_drop(10, 3)


async def test_job_only_in_unpaged_feed_is_indirect() -> None:
    """Tin chỉ có trong feed không phân trang ("việc làm nổi bật", có thể xoay vòng): bộ khung
    ghi là tin gián tiếp, lượt poll không thấy lại thì không coi là đã gỡ."""
    feed = "https://api.acme.vn/jobs/featured"
    fetcher = site(dom_ids=[1, 2, 3], api_ids=[1, 2])
    fetcher.add(job_url(4), detail(4))
    fetcher.rendered[LISTING] = dataclasses.replace(
        fetcher.rendered[LISTING],
        captured=(
            CapturedJson(url=API, method="GET", status=200, body=api_body([1, 2])),
            CapturedJson(
                url=feed, method="POST", status=200, body=api_body([3, 4]), request_json={}
            ),
        ),
    )
    spec, jobs = await build(fetcher)
    by_url = {j.url: j.external_id for j in jobs}
    assert set(by_url) == {job_url(n) for n in (1, 2, 3, 4)}
    assert [(a.url, a.paged) for a in spec.apis] == [(API, True), (feed, False)]
    assert spec.indirect == [by_url[job_url(4)]]  # tin 3 còn có trên trang: không gián tiếp
    assert spec.api_only == [LISTING]  # trang + feed + API có đủ mọi tin của trang


def test_known_jobs_match_listing_links_without_trailing_slash() -> None:
    """URL đã lưu là canonical `.../abc/`, link trên trang danh sách là `.../abc`: vẫn là tin
    đã biết (không mở lại trang chi tiết mỗi lượt poll)."""
    known = KnownJobs.of_refs(
        [("careers", "k1", "https://www.misa.vn/tuyen-dung/5920/lap-trinh-vien-web-c-net/")]
    )
    assert known.take(url="https://www.misa.vn/tuyen-dung/5920/lap-trinh-vien-web-c-net")
    assert known.take(url="https://WWW.misa.vn/tuyen-dung/5920/lap-trinh-vien-web-c-net/#apply")
    assert known.take(url="https://www.misa.vn/tuyen-dung/5921/khac") is None
    root = KnownJobs.of_refs([("careers", "k2", "https://jobs.example.vn/")])
    assert root.take(url="https://jobs.example.vn") is not None
