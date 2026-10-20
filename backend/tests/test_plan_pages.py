"""Bộ khung 3–10 trang mỗi doanh nghiệp: lượt build chia trang cho các nguồn
(`modules.crawl.plans.allocate_pages`), lượt poll chỉ đọc đúng phần trang của nguồn."""

from __future__ import annotations

import json
from typing import Any

import pytest

from jobcrawler.common.schemas import RawJob
from jobcrawler.modules.crawl.plans import allocate_pages, built_plans
from jobcrawler.pipeline.collect import SourceReport
from jobcrawler.services.fakes import FakeChatClient, FakeFetcher
from jobcrawler.services.llm import LLMClient
from jobcrawler.sources.base import KnownJobs, SourceLimits, SourceSession
from jobcrawler.sources.careers import CareersSource
from jobcrawler.sources.plan import ApiSpec, PlanSpec

SITE = "https://careers.acme.vn"
LIST = f"{SITE}/tuyen-dung"
JD = (
    "Mô tả công việc: hạch toán, lập báo cáo tài chính hằng tháng, đối chiếu công nợ với khách "
    "hàng và nhà cung cấp. Yêu cầu: tốt nghiệp chuyên ngành kế toán, ít nhất hai năm kinh nghiệm. "
    "Quyền lợi: lương tháng 13, bảo hiểm đầy đủ, du lịch hằng năm."
)


@pytest.mark.parametrize(
    ("demands", "growable", "expected"),
    [
        ([2], [True], [10]),  # site 2 trang: phần dư để dành cho trang sau mới phát sinh
        ([20], [True], [10]),  # site 20 trang: tối đa 10
        ([5, 5, 1], [True, True, False], [5, 4, 1]),  # mỗi nguồn ≥ 3 rồi chia vòng
        ([5, 5, 5, 5], [True] * 4, [3, 3, 3, 1]),  # nhiều nguồn: tổng vẫn ≤ 10
        ([1, 1], [False, False], [1, 1]),  # chỉ bảng tin ATS: không cho thêm trang thừa
        ([0, 4], [True, True], [0, 10]),  # nguồn hỏng (nhu cầu 0) không nhận trang
    ],
)
def test_allocate_pages(demands: list[int], growable: list[bool], expected: list[int]) -> None:
    assert allocate_pages(demands, max_pages=10, min_pages=3, growable=growable) == expected


def test_demand() -> None:
    spec = PlanSpec(
        listings=[LIST, f"{LIST}?page=2", f"{LIST}?page=3"],
        hubs=[f"{LIST}/ha-noi"],
        api_only=[LIST],  # trang SPA có API thay: không mở
        apis=[ApiSpec(url="https://api.acme.vn/jobs", total=45, size=20)],
    )
    assert spec.demand() == 3 + 3  # 3 trang (bỏ trang có API thay) + 3 trang API
    assert PlanSpec(ats={"kind": "greenhouse", "key": "acme"}, jobs=300).demand() == 1
    assert PlanSpec(ats={"kind": "workday", "key": "acme/x"}, jobs=45).demand() == 3


def test_built_plans_share_the_company_budget() -> None:
    reports = [
        SourceReport(name="careers:careers.acme.vn", url=LIST, jobs=5, keys=["a"]),
        SourceReport(
            name="careers:www.acme.vn", url="https://www.acme.vn/jobs", jobs=5, keys=["b"]
        ),
    ]
    reports[0].plan = PlanSpec(listings=[f"{LIST}?page={n}" for n in range(1, 9)], jobs=5)
    reports[1].plan = PlanSpec(listings=["https://www.acme.vn/jobs"], jobs=5)
    plans = built_plans(reports, max_pages=10, min_pages=3)
    # 3 + 1 trước, rồi đủ nhu cầu (8 + 1); trang dư thứ 10 cho trang careers đầu.
    assert [p.spec.pages for p in plans] == [9, 1]


# --- lượt poll chỉ đọc phần trang của nguồn ----------------------------------------------------


def job_url(n: int) -> str:
    return f"{SITE}/tuyen-dung/ke-toan-tong-hop-{n}"


def listing(page: int, pages: int, ids: list[int]) -> str:
    links = "".join(f'<li><a href="{job_url(n)}">Kế toán tổng hợp {n}</a></li>' for n in ids)
    more = f'<a href="{LIST}?page={page + 1}">Trang sau</a>' if page < pages else ""
    return f"<html><body><h1>Tuyển dụng</h1><ul>{links}</ul>{more}</body></html>"


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


def page_url(page: int) -> str:
    return LIST if page == 1 else f"{LIST}?page={page}"


def site(pages: int, per_page: int = 2, first: int = 1) -> FakeFetcher:
    """Danh sách `pages` trang, mỗi trang `per_page` tin, tin mới nhất ở trang đầu."""
    fetcher = FakeFetcher()
    n = first
    for page in range(1, pages + 1):
        ids = list(range(n, n + per_page))
        n += per_page
        fetcher.add(page_url(page), listing(page, pages, ids))
        for i in ids:
            fetcher.add(job_url(i), detail(i))
    return fetcher


def session(fetcher: FakeFetcher, **kw: Any) -> SourceSession:
    return SourceSession(
        "careers:careers.acme.vn", fetcher, LLMClient(FakeChatClient(["{}"]), model=None), **kw
    )


async def test_poll_reads_only_the_pages_of_its_plan() -> None:
    fetcher = site(pages=5)
    s = session(fetcher)
    jobs: list[RawJob] = [job async for job in CareersSource(LIST).jobs(s)]
    assert len(s.plan.listings) == 5
    assert s.plan.demand() == 5

    # Tin mới lên đầu trang 1 (mã 0); bộ khung chỉ cho nguồn 3 trang.
    fetcher.add(page_url(1), listing(1, 5, [0, 1]))
    fetcher.add(job_url(0), detail(0))
    fetcher.calls.clear()
    known = KnownJobs.of_refs((j.source, j.external_id, j.url) for j in jobs)
    poll = session(
        fetcher, mode="poll", plan=s.plan, known=known, limits=SourceLimits(poll_pages=3)
    )
    polled = [job async for job in CareersSource(LIST).jobs(poll)]

    opened = [url for _, url, _ in fetcher.calls]
    assert [u for u in opened if u.startswith(LIST) and "ke-toan" not in u] == [
        page_url(1),
        page_url(2),
        page_url(3),
    ]
    assert opened.count(job_url(0)) == 1  # chỉ mở trang tin mới
    assert "https://careers.acme.vn/tuyen-dung/ke-toan-tong-hop-0" in {j.url for j in polled}
    # Không đọc trọn: không đóng việc ở trang 4–5, cũng không coi là bộ khung hỏng.
    assert "plan_pages" in poll.truncated
    assert poll.broken is None


async def test_poll_within_its_pages_is_complete() -> None:
    fetcher = site(pages=3)
    s = session(fetcher)
    jobs = [job async for job in CareersSource(LIST).jobs(s)]
    known = KnownJobs.of_refs((j.source, j.external_id, j.url) for j in jobs)
    poll = session(
        fetcher, mode="poll", plan=s.plan, known=known, limits=SourceLimits(poll_pages=10)
    )
    [job async for job in CareersSource(LIST).jobs(poll)]
    assert poll.truncated == []
    assert poll.broken is None
