"""Bộ khung trang careers SPA (sources/careers.py + sources/plan.py): lượt build ghi API bắt được,
lượt poll chỉ gọi API khi API có đủ mọi tin của trang, không thì mở lại trang."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import Any

import pytest

from jobcrawler.common.schemas import RawJob
from jobcrawler.modules.crawl.crawl_service import suspicious_drop
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


def plain_listing(ids: list[int]) -> str:
    links = "".join(f'<li><a href="{job_url(n)}">Kế toán tổng hợp {n}</a></li>' for n in ids)
    return f"<html><body><main><h1>Tuyển dụng</h1><ul>{links}</ul></main></body></html>"


async def test_poll_reads_one_language_copy_of_a_listing() -> None:
    """Bộ khung ghi cả /jobs, /en/jobs, /ja/jobs (cùng các tin): lượt poll chỉ đọc bản đầu."""
    fetcher = FakeFetcher()
    for url in (LISTING, f"{SITE}/en/jobs", f"{SITE}/ja/jobs"):
        fetcher.add(url, plain_listing([1, 2]))
    for n in (1, 2):
        fetcher.add(job_url(n), detail(n))
    spec, jobs = await build(fetcher)
    spec.listings = [LISTING, f"{SITE}/en/jobs", f"{SITE}/ja/jobs"]
    fetcher.calls.clear()
    await poll(fetcher, spec, jobs)
    opened = [u for kind, u, _ in fetcher.calls]
    assert LISTING in opened
    assert f"{SITE}/en/jobs" not in opened
    assert f"{SITE}/ja/jobs" not in opened


async def test_poll_does_not_wait_long_for_an_overloaded_llm(monkeypatch: Any) -> None:
    """Trang tin ít dấu hiệu JD cần LLM: LLM quá tải thì lượt poll bóc bằng rule (trang trông như
    tin) thay vì chờ tới hết giờ cả lượt."""
    from jobcrawler.sources import careers

    monkeypatch.setattr(careers, "POLL_LLM_SECONDS", 0.05)
    fetcher = FakeFetcher()
    fetcher.add(LISTING, plain_listing([1]))
    page = (
        "<html><body><main><h1>Kế toán tổng hợp 2</h1><p>Mô tả công việc: lập báo cáo thuế.</p>"
        "<p>Mức lương: 15 triệu. Liên hệ phòng nhân sự.</p></main></body></html>"
    )
    fetcher.add(job_url(1), detail(1))
    fetcher.add(job_url(2), page)
    spec, jobs = await build(fetcher)
    fetcher.add(LISTING, plain_listing([1, 2]))
    slow = LLMClient(FakeChatClient(['{"is_job_posting": true}'], delay=5), model="qwen-test")
    s = session(fetcher, mode="poll", plan=spec, known=known(jobs))
    s.llm = slow
    polled = [job async for job in CareersSource(LISTING).jobs(s)]
    new = [j for j in polled if j.url == job_url(2)]
    assert len(new) == 1
    assert new[0].method.value == "heuristic"
    assert s.broken is None


def test_poll_deadline_after_reading_the_listing_is_not_a_plan_failure() -> None:
    from jobcrawler.common.schemas import FetchOutcome
    from jobcrawler.database.models import CrawlPlan
    from jobcrawler.modules.crawl.crawl_service import _check  # pyright: ignore[reportPrivateUsage]
    from jobcrawler.pipeline.collect import SourceReport

    plan = CrawlPlan(source="careers:x", job_keys=["a"], failures=2, spec={})
    late = SourceReport("careers:x", "https://x.vn", outcome=FetchOutcome.TIMEOUT, listed=True)
    late.keys = ["a", "b"]
    check = _check(plan, late, max_failures=3)
    assert check.ok
    assert check.broken is None
    assert check.new == {"b"}
    assert not check.complete  # không đóng việc theo lượt chưa trọn
    stuck = SourceReport("careers:x", "https://x.vn", outcome=FetchOutcome.TIMEOUT)
    assert _check(plan, stuck, max_failures=3).broken is not None


INTRO = (
    "Chúng tôi luôn chào đón những ứng viên tài năng, nhiệt huyết và mong muốn phát triển cùng "
    "công ty. Môi trường làm việc chuyên nghiệp, chế độ đãi ngộ cạnh tranh, nhiều cơ hội đào tạo "
    "và thăng tiến. Hiện công ty chưa có vị trí tuyển dụng nào, vui lòng quay lại sau."
)


def static_page(extra: str = "") -> str:
    return (
        f'<html><body><h1>Tuyển dụng</h1><p>{INTRO}</p><a href="{SITE}/gioi-thieu">Giới thiệu</a>'
        f"{extra}</body></html>"
    )


async def test_render_that_adds_nothing_is_not_kept_for_polls() -> None:
    """Trang tĩnh hiện không có tin: lượt build vẫn thử mở bằng trình duyệt, nhưng DOM không thêm
    gì nên bộ khung không ghi `rendered` và lượt poll chỉ đọc HTTP."""
    fetcher = FakeFetcher()
    fetcher.add(LISTING, static_page())
    fetcher.add_rendered(LISTING, static_page('<div class="cookie">Đồng ý cookie</div>'))
    spec, _ = await build(fetcher)
    assert "render" in {kind for kind, _, _ in fetcher.calls}
    assert spec.listings == [LISTING]
    assert spec.rendered == []
    fetcher.calls.clear()
    await poll(fetcher, spec, [])
    assert [kind for kind, _, _ in fetcher.calls] == ["fetch"]


async def test_render_that_adds_text_or_links_is_kept() -> None:
    from jobcrawler.sources.careers import Page, render_adds, scan_listing

    http = scan_listing(Page(LISTING, static_page()))
    links = "".join(f'<a href="{SITE}/p/{n}">Mục {n}</a>' for n in range(3))
    assert render_adds(http, scan_listing(Page(LISTING, static_page(links), rendered=True)))
    text = f"<p>{JD}</p><p>{JD}</p>"
    assert render_adds(http, scan_listing(Page(LISTING, static_page(text), rendered=True)))
    noise = f'<a href="{SITE}/p/1">Mục 1</a><p>Đồng ý cookie</p>'
    assert not render_adds(http, scan_listing(Page(LISTING, static_page(noise), rendered=True)))


async def test_poll_llm_call_past_the_limit_finishes_in_background(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lượt poll thôi chờ LLM sau POLL_LLM_SECONDS nhưng lời gọi chạy nốt (câu trả lời vào cache
    cho lượt sau); lượt build thì chờ."""
    from jobcrawler.sources import careers

    monkeypatch.setattr(careers, "POLL_LLM_SECONDS", 0.01)
    done: list[int] = []

    async def slow() -> int:
        await asyncio.sleep(0.05)
        done.append(1)
        return 1

    with pytest.raises(TimeoutError):
        await careers.poll_bounded(session(FakeFetcher(), mode="poll"), slow())
    assert done == []
    assert careers._FINISHING  # pyright: ignore[reportPrivateUsage]
    await asyncio.sleep(0.1)
    assert done == [1]
    assert not careers._FINISHING  # pyright: ignore[reportPrivateUsage]
    assert await careers.poll_bounded(session(FakeFetcher()), slow()) == 1


async def test_job_board_pages_are_not_read_as_listings() -> None:
    """Trang chính của nguồn là trang của sàn / chuyên trang việc làm (link tới hồ sơ nhiều nhà
    tuyển dụng): lượt build báo `job_board_page` (không ghi bộ khung); lượt poll không mở trang trên
    chính sàn, không mở link tin dẫn vào host sàn đã ghi (`PlanSpec.boards`)."""
    import pytest

    from jobcrawler.sources.base import SourceError
    from jobcrawler.sources.careers import JOB_BOARD_PAGE

    employers = "".join(
        f'<a href="{SITE}/nha-tuyen-dung/cong-ty-{n}.html">Công ty TNHH Số {n}</a>'
        for n in range(3)
    )
    fetcher = FakeFetcher()
    fetcher.add(LISTING, rendered_dom([1, 2]).replace("</ul>", f"</ul>{employers}"))
    for n in (1, 2):
        fetcher.add(job_url(n), detail(n))
    with pytest.raises(SourceError, match=JOB_BOARD_PAGE):
        await build(fetcher)

    board = "https://jobsgo.vn/viec-lam.html"
    other = "https://vieclam.bao.vn/tim-viec-lam/ke-toan-tong-hop-77.html"
    fetcher.add(LISTING, rendered_dom([1]).replace("</ul>", f'<li><a href="{other}">Kế toán</a>'))
    spec = PlanSpec(listings=[board, LISTING], boards=["vieclam.bao.vn"], jobs=1)
    s = session(fetcher, mode="poll", plan=spec)
    fetcher.calls.clear()
    polled = [job async for job in CareersSource(board).jobs(s)]
    opened = [url for _, url, _ in fetcher.calls]
    assert board not in opened
    assert other not in opened
    assert [j.url for j in polled] == [job_url(1)]
