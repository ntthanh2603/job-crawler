from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from jobcrawler.common.pages import Link
from jobcrawler.common.schemas import ExtractionMethod, FetchOutcome, RawJob
from jobcrawler.services.fakes import FakeChatClient, FakeFetcher
from jobcrawler.services.llm import LLMClient
from jobcrawler.services.net.http import CapturedJson
from jobcrawler.services.net.kv import MemoryKV
from jobcrawler.sources.base import KnownJobs, SourceLimits, SourceSession
from jobcrawler.sources.careers import (
    CareersSource,
    JobLink,
    Page,
    _stop,  # pyright: ignore[reportPrivateUsage]
    clean_card_text,
    hub_page,
    jd_signals,
    job_links,
    labeled_fields,
    listing_links,
    pick_title,
    scan_listing,
    slug_hint,
    split_markdown,
)

FIXTURES = Path(__file__).parent / "fixtures" / "careers"
BASE = "https://careers.base.vn"
JD = """<html><body><main><h1>{title}</h1>
<h2>Mô tả công việc</h2><ul><li>Phát triển tính năng mới cho sản phẩm.</li></ul>
<h2>Yêu cầu</h2><ul><li>Kinh nghiệm: 2 năm với Python.</li></ul>
<h2>Quyền lợi</h2><ul><li>Lương tháng 13, bảo hiểm.</li></ul>
<p><strong>Mức lương:</strong> 20 - 30 triệu</p><p>Hạn nộp: 30/11/2026</p>
</main></body></html>"""


def html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def disabled_llm() -> LLMClient:
    return LLMClient(FakeChatClient(["{}"]), model=None)


def fake_llm(*replies: dict[str, Any]) -> tuple[LLMClient, FakeChatClient]:
    chat = FakeChatClient([json.dumps(r, ensure_ascii=False) for r in replies])
    return LLMClient(chat, model="qwen-test"), chat


def session(fetcher: FakeFetcher, llm: LLMClient | None = None, **limits: Any) -> SourceSession:
    return SourceSession("careers:test", fetcher, llm or disabled_llm(), SourceLimits(**limits))


async def run(source: CareersSource, s: SourceSession) -> list[RawJob]:
    return [job async for job in source.jobs(s)]


# --- rule ------------------------------------------------------------------------------------


def test_job_links_on_base_careers_page() -> None:
    page = Page(f"{BASE}/", html("base_vn/careers.html"))
    scan = scan_listing(page)
    # Chỉ trang chi tiết /job/...; bỏ link lọc theo phòng ban/văn phòng và bài blog life.base.vn.
    assert len(scan.job_links) == 5
    assert all(link.url.startswith(f"{BASE}/job/") for link in scan.job_links)
    assert scan.job_links[0].title == (
        "[HN] Account Executive / Business Development (B2B Sales - Renewal, Upsell, Cross - sell)"
    )
    assert scan.next_pages == [f"{BASE}/alljobs"]  # "Xem tất cả"


def test_job_links_scoring() -> None:
    links = [
        Link("https://a.vn/tuyen-dung/nhan-vien-kinh-doanh", "Nhân viên kinh doanh"),
        Link("https://a.vn/tuyen-dung/ke-toan-123", "Xem chi tiết"),
        Link("https://a.vn/tuyen-dung/detail?id=99", "Kỹ sư cầu đường"),
        Link("https://a.vn/tuyen-dung?page=2", "Trang 2"),
        Link("https://a.vn/tuyen-dung/tim-kiem?tukhoa=ke+toan", "Kế toán tổng hợp"),
        Link("https://a.vn/tuyen-dung/all-jobs", "Tất cả vị trí"),
        Link("https://a.vn/tin-tuc/tuyen-dung-thang-10", "Tin tuyển dụng tháng 10"),
        Link("https://a.vn/tuyen-dung/jd.pdf", "Mô tả công việc PDF"),
        Link("https://b.vn/tuyen-dung/ke-toan-1", "Kế toán ở site khác"),
        Link("https://a.vn/gioi-thieu", "Giới thiệu công ty"),
    ]
    found = job_links(links, "https://a.vn/tuyen-dung")
    assert [(j.url, j.title) for j in found] == [
        ("https://a.vn/tuyen-dung/nhan-vien-kinh-doanh", "Nhân viên kinh doanh"),
        ("https://a.vn/tuyen-dung/ke-toan-123", None),
        ("https://a.vn/tuyen-dung/detail?id=99", "Kỹ sư cầu đường"),
    ]
    assert listing_links(links, "https://a.vn/tuyen-dung") == [
        "https://a.vn/tuyen-dung?page=2",
        "https://a.vn/tuyen-dung/all-jobs",
    ]


def test_recruitment_section_detail_links_are_jobs() -> None:
    """Viettel (DOM sau khi render): /recruitment-information/detail/<mã>."""
    base = "https://tuyendung.viettel.vn"
    links = [
        Link(f"{base}/recruitment-information/detail/44404", "Kỹ sư phát triển phần mềm"),
        Link(f"{base}/recruitment-information/detail/44398", "Chuyên viên phân tích dữ liệu"),
        Link(f"{base}/recruitment-event/search", "Sự kiện tuyển dụng"),
        Link(f"{base}/page/page-recruitmentEvent", "Sự kiện"),
    ]
    found = job_links(links, f"{base}/recruitment-information/search")
    assert [j.url for j in found] == [
        f"{base}/recruitment-information/detail/44404",
        f"{base}/recruitment-information/detail/44398",
    ]


def test_listing_root_page_does_not_treat_every_link_as_child() -> None:
    links = [
        Link("https://jobs.a.vn/ve-chung-toi-va-van-hoa", "Về chúng tôi và văn hoá"),
        Link("https://jobs.a.vn/job/ke-toan-tong-hop-12", "Kế toán tổng hợp"),
    ]
    assert [j.url for j in job_links(links, "https://jobs.a.vn/")] == [
        "https://jobs.a.vn/job/ke-toan-tong-hop-12"
    ]


def test_jd_signals_and_labeled_fields() -> None:
    md = (
        "# Kỹ sư\n## Mô tả công việc\n- x\n## Yêu cầu\n- y\n## Quyền lợi\n- z\n"
        "**Mức lương:** 10 - 15 triệu\n- Hạn nộp hồ sơ: 30/11/2026\nĐịa điểm:\n**Hà Nội**\n"
        "Kinh nghiệm: 2 năm\nGhi chú: bỏ qua"
    )
    assert jd_signals(md) >= 5
    assert labeled_fields(md) == {
        "salary_raw": "10 - 15 triệu",
        "expires_raw": "30/11/2026",
        "location": "Hà Nội",
        "experience_raw": "2 năm",
    }
    assert jd_signals("# Tin tức\nCông ty vừa khai trương văn phòng mới.") == 0


# --- nguồn -----------------------------------------------------------------------------------


async def test_base_vn_careers_follows_view_all_and_reads_json_ld() -> None:
    job_url = f"{BASE}/job/hn-business-development-executive-junior-b2b-sales-1yoe-29675"
    fetcher = FakeFetcher()
    fetcher.add(f"{BASE}/", html("base_vn/careers.html"))
    fetcher.add(f"{BASE}/alljobs", html("base_vn/alljobs.html"))
    fetcher.add(job_url, html("base_vn/job_29675.html"))
    s = session(fetcher)
    jobs = await run(CareersSource(f"{BASE}/"), s)

    assert [j.url for j in jobs] == [job_url]  # các trang chi tiết khác trả 404
    assert jobs[0].method is ExtractionMethod.JSONLD
    urls = [url for _, url, _ in fetcher.calls]
    assert urls[:2] == [f"{BASE}/", f"{BASE}/alljobs"]
    details = urls[2:]
    assert len(details) == len(set(details)) == 25  # 5 link trang đầu nằm trong 25 link
    assert all(kind == "fetch" for kind, _, _ in fetcher.calls)  # không cần trình duyệt
    assert len(s.records) == 27
    assert [r.items for r in s.records if r.items] == [1]  # trang chi tiết có JSON-LD


async def test_vnpay_heuristic_parser_without_llm() -> None:
    job_url = "https://tuyendung.vnpay.vn/tuyen-dung/truong-nhom-nhan-su.html"
    fetcher = FakeFetcher()
    fetcher.add("https://tuyendung.vnpay.vn/", html("vnpay/home.html"))
    fetcher.add(job_url, html("vnpay/job_truong_nhom_nhan_su.html"))
    jobs = await run(CareersSource("https://tuyendung.vnpay.vn/"), session(fetcher))

    [job] = jobs
    assert job.method is ExtractionMethod.HEURISTIC
    assert job.title == "Trưởng nhóm Nhân sự"
    assert job.url == job_url
    assert job.description_format == "markdown"
    assert job.description
    assert "Mô tả công việc" in job.description
    assert job.locations == ["22 Láng Hạ - Phường Láng - Thành phố Hà Nội"]
    assert job.salary_raw == "Thỏa thuận"


async def test_momo_listing_rendered_when_http_shows_nothing() -> None:
    """momo.careers/jobs nạp danh sách bằng JavaScript: HTML gốc không có link tin."""
    listing = "https://momo.careers/jobs"
    job_url = "https://momo.careers/jobs/senior-security-engineer-19312"
    fetcher = FakeFetcher()
    fetcher.add(listing, html("momo/careers_jobs.html"))
    fetcher.add_rendered_file(listing, FIXTURES / "momo/careers_jobs_rendered.html")
    fetcher.add(job_url, html("momo/job_19312.html"))
    s = session(fetcher)
    jobs = await run(CareersSource(listing), s)

    assert [j.url for j in jobs] == [job_url]
    assert jobs[0].method is ExtractionMethod.HEURISTIC
    assert jobs[0].title
    kinds = [(kind, url) for kind, url, _ in fetcher.calls[:2]]
    assert kinds == [("fetch", listing), ("render", listing)]
    assert fetcher.calls[1][2]["wait_until"] == "networkidle"
    assert s.rendered_pages == 1


async def test_vng_llm_listing_fallback() -> None:
    """VNG: thẻ tin không phải `<a href>` → rule không thấy gì, LLM đọc trang danh sách."""
    url = "https://career.vng.com.vn/vi/tim-kiem-viec-lam"
    fetcher = FakeFetcher()
    fetcher.add_rendered_file(url, FIXTURES / "vng/search_rendered.html")
    fetcher.add(url, html("vng/search_rendered.html"))
    llm, chat = fake_llm(
        {
            "is_job_listing": True,
            "jobs": [
                {"title": "Technical Support Engineer, GreenNode", "location": "Thành phố Hà Nội"},
                {"title": "  ", "link": None},
                {"title": "Link ngoài danh sách", "link": 999},
            ],
            "listing_links": [],
        }
    )
    jobs = await run(CareersSource(url), session(fetcher, llm))

    assert [(j.title, j.locations, j.method) for j in jobs] == [
        ("Technical Support Engineer, GreenNode", ["Thành phố Hà Nội"], ExtractionMethod.LLM),
        ("Link ngoài danh sách", [], ExtractionMethod.LLM),
    ]
    assert jobs[0].url == url
    assert jobs[0].external_id != jobs[1].external_id
    # Trang dài được đọc theo từng đoạn; tin trùng tên giữa các đoạn được gộp.
    assert len(chat.calls) == 3
    assert any("Technical Support Engineer" in c["messages"][1]["content"] for c in chat.calls)


async def test_llm_decides_unclear_detail_page() -> None:
    listing = "https://a.vn/tuyen-dung"
    page = (
        '<html><body><a href="/tuyen-dung/ky-su-cau-duong-01">Kỹ sư cầu đường</a>'
        '<a href="/tuyen-dung/thong-bao-nghi-le-02">Thông báo nghỉ lễ</a></body></html>'
    )
    fetcher = FakeFetcher()
    fetcher.add(listing, page)
    fetcher.add(
        "https://a.vn/tuyen-dung/ky-su-cau-duong-01",
        "<main><h1>Kỹ sư cầu đường</h1><p>Làm việc tại công trường Bình Dương, "
        "lương thoả thuận, nhận hồ sơ đến hết tháng.</p></main>",
    )
    fetcher.add(
        "https://a.vn/tuyen-dung/thong-bao-nghi-le-02",
        "<main><h1>Thông báo nghỉ lễ</h1><p>Công ty nghỉ lễ Quốc khánh từ ngày 1/9.</p></main>",
    )
    llm, chat = fake_llm(
        {
            "is_job_posting": True,
            "title": "Kỹ sư cầu đường",
            "locations": ["Bình Dương"],
            "salary": "Thoả thuận",
            "deadline": "hết tháng",
        },
        {"is_job_posting": False},
    )
    jobs = await run(CareersSource(listing), session(fetcher, llm, detail_concurrency=1))

    [job] = jobs
    assert job.method is ExtractionMethod.LLM
    assert (job.title, job.locations, job.salary_raw, job.expires_raw) == (
        "Kỹ sư cầu đường",
        ["Bình Dương"],
        "Thoả thuận",
        "hết tháng",
    )
    assert len(chat.calls) == 2

    # Có cache: lượt sau trang không đổi thì không hỏi LLM lại.
    llm.cache = MemoryKV()
    first = await run(CareersSource(listing), session(fetcher, llm, detail_concurrency=1))
    calls = len(chat.calls)
    again = await run(CareersSource(listing), session(fetcher, llm, detail_concurrency=1))
    assert [j.title for j in again] == [j.title for j in first]
    assert len(chat.calls) == calls

    # Không có LLM: trang không rõ là tin bị bỏ (ưu tiên độ chính xác).
    assert await run(CareersSource(listing), session(fetcher)) == []


async def test_sub_listing_pages_and_pagination() -> None:
    listing = "https://a.vn/tuyen-dung"
    fetcher = FakeFetcher()
    fetcher.add(
        listing,
        '<a href="/tuyen-dung/khoi-cong-nghe-ky-thuat">Khối công nghệ kỹ thuật</a>'
        '<a href="/tuyen-dung?page=2">2</a>',
    )
    fetcher.add(
        "https://a.vn/tuyen-dung?page=2",
        '<a href="/tuyen-dung/ke-toan-noi-bo-7">Kế toán nội bộ</a>',
    )
    fetcher.add(
        "https://a.vn/tuyen-dung/khoi-cong-nghe-ky-thuat",
        "<main><h1>Khối công nghệ</h1>"
        + "".join(
            f'<a href="/tuyen-dung/lap-trinh-vien-{i}">Lập trình viên {i}</a>' for i in range(3)
        )
        + "</main>",
    )
    for slug, title in [("ke-toan-noi-bo-7", "Kế toán nội bộ")] + [
        (f"lap-trinh-vien-{i}", f"Lập trình viên {i}") for i in range(3)
    ]:
        fetcher.add(f"https://a.vn/tuyen-dung/{slug}", JD.format(title=title))

    jobs = await run(CareersSource(listing), session(fetcher))
    assert sorted(j.title for j in jobs) == [
        "Kế toán nội bộ",
        "Lập trình viên 0",
        "Lập trình viên 1",
        "Lập trình viên 2",
    ]
    job = next(j for j in jobs if j.title == "Kế toán nội bộ")
    assert job.salary_raw == "20 - 30 triệu"
    assert job.expires_raw == "30/11/2026"
    assert job.experience_raw == "2 năm với Python."


async def test_detail_page_limit() -> None:
    listing = "https://a.vn/tuyen-dung"
    fetcher = FakeFetcher()
    fetcher.add(
        listing,
        "".join(f'<a href="/tuyen-dung/vi-tri-so-{i}">Vị trí số {i}</a>' for i in range(10)),
    )
    for i in range(10):
        fetcher.add(f"https://a.vn/tuyen-dung/vi-tri-so-{i}", JD.format(title=f"Vị trí số {i}"))
    jobs = await run(
        CareersSource(listing), session(fetcher, max_detail_pages=4, max_rendered_pages=0)
    )
    assert [j.title for j in jobs] == [f"Vị trí số {i}" for i in range(4)]
    assert len(fetcher.calls) == 5


async def test_capped_or_failed_pages_mark_source_truncated() -> None:
    listing = "https://a.vn/tuyen-dung"
    fetcher = FakeFetcher()
    fetcher.add(
        listing,
        "".join(f'<a href="/tuyen-dung/vi-tri-so-{i}">Vị trí số {i}</a>' for i in range(6)),
    )
    for i in range(6):
        fetcher.add(f"https://a.vn/tuyen-dung/vi-tri-so-{i}", JD.format(title=f"Vị trí số {i}"))
    capped = session(fetcher, max_detail_pages=4)
    await run(CareersSource(listing), capped)
    assert capped.truncated == ["detail_pages"]

    whole = session(fetcher)
    assert len(await run(CareersSource(listing), whole)) == 6
    assert whole.truncated == []

    # Trang chi tiết lỗi tạm (5xx) → chưa xong; 404 (tin đã gỡ) thì không.
    flaky = FakeFetcher()
    flaky.add(
        listing,
        '<a href="/tuyen-dung/vi-tri-so-1">Vị trí số 1</a>'
        '<a href="/tuyen-dung/vi-tri-so-2">Vị trí số 2</a>',
    )
    flaky.add("https://a.vn/tuyen-dung/vi-tri-so-1", "lỗi", status=503)
    s503 = session(flaky)
    assert await run(CareersSource(listing), s503) == []
    assert s503.truncated == ["page_error"]


async def test_known_details_are_reused_without_counting_toward_the_cap() -> None:
    listing = "https://a.vn/tuyen-dung"
    fetcher = FakeFetcher()
    fetcher.add(
        listing,
        "".join(f'<a href="/tuyen-dung/vi-tri-so-{i}">Vị trí số {i}</a>' for i in range(6)),
    )
    for i in range(6):
        fetcher.add(f"https://a.vn/tuyen-dung/vi-tri-so-{i}", JD.format(title=f"Vị trí số {i}"))
    # Lượt trước đã tải chi tiết tin 0–3.
    known = KnownJobs.of(
        (
            RawJob(
                source="careers",
                external_id=f"old-{i}",
                url=f"https://a.vn/tuyen-dung/vi-tri-so-{i}",
                title=f"Vị trí số {i} (đã lưu)",
                method=ExtractionMethod.HEURISTIC,
            ),
            f"https://a.vn/tuyen-dung/vi-tri-so-{i}",
        )
        for i in range(4)
    )
    s = session(fetcher, max_detail_pages=2)
    s.known = known
    jobs = await run(CareersSource(listing), s)
    assert sorted(j.title for j in jobs) == [
        *(f"Vị trí số {i} (đã lưu)" for i in range(4)),
        "Vị trí số 4",
        "Vị trí số 5",
    ]
    opened = [u for kind, u, _ in fetcher.calls if kind == "fetch" and u != listing]
    assert sorted(opened) == [
        "https://a.vn/tuyen-dung/vi-tri-so-4",
        "https://a.vn/tuyen-dung/vi-tri-so-5",
    ]
    assert s.truncated == []
    assert known.used == {("careers", f"old-{i}") for i in range(4)}


TGDD = "https://vieclam.thegioididong.com"


def test_hub_pages_are_category_pages_not_jobs() -> None:
    category = scan_listing(
        Page(
            f"{TGDD}/tuyen-dung/ban-hang-thu-ngan-ky-thuat-kho-sieu-thi", html("tgdd/category.html")
        )
    )
    assert hub_page(category.page.url, category.job_links)
    province = scan_listing(Page(f"{TGDD}/tuyen-dung-thanh-pho-ha-noi", html("tgdd/province.html")))
    assert hub_page(province.page.url, province.job_links)
    # Trang tin có mã: khối "việc làm khác" không làm nó thành danh mục.
    job = scan_listing(
        Page(f"{TGDD}/tuyen-dung/nhan-vien-sieu-thi-bach-hoa-xanh-45", html("tgdd/job_45.html"))
    )
    assert not hub_page(job.page.url, job.job_links)
    related = [JobLink(f"https://a.vn/jobs/vi-tri-{i}-{100 + i}", None) for i in range(5)]
    assert not hub_page("https://a.vn/jobs/ke-toan-99", related)
    assert hub_page("https://a.vn/jobs/phong-ke-toan", related)


async def test_category_pages_are_followed_to_their_jobs() -> None:
    """vieclam.thegioididong.com: trang chủ careers chỉ dẫn tới trang ngành nghề / tỉnh thành,
    tin nằm trên các trang đó."""
    fetcher = FakeFetcher()
    fetcher.add(f"{TGDD}/", html("tgdd/home.html"))
    category = f"{TGDD}/tuyen-dung/ban-hang-thu-ngan-ky-thuat-kho-sieu-thi"
    fetcher.add(category, html("tgdd/category.html"))
    job = f"{TGDD}/tuyen-dung/nhan-vien-sieu-thi-bach-hoa-xanh-45"
    fetcher.add(job, html("tgdd/job_45.html"))
    s = session(fetcher, max_detail_pages=200, max_rendered_pages=0)
    jobs = await run(CareersSource(f"{TGDD}/"), s)
    # Trang ngành nghề không bị lưu thành tin; tin trên đó được mở.
    assert [j.url for j in jobs] == [job]
    assert jobs[0].title == "Nhân Viên Siêu Thị Bách Hóa Xanh"
    opened = {u for kind, u, _ in fetcher.calls if kind == "fetch"}
    assert job in opened
    assert f"{TGDD}/tuyen-dung/quan-ly-sieu-thi-bach-hoa-xanh-646" in opened


def captured(name: str) -> CapturedJson:
    """Phản hồi API bắt được khi mở trang bằng trình duyệt (tests/fixtures/careers/json/)."""
    data = json.loads((FIXTURES / "json" / name).read_text(encoding="utf-8"))
    return CapturedJson(
        url=data["url"],
        method=data["method"],
        status=200,
        body=json.dumps(data["body"], ensure_ascii=False).encode(),
        request_json=data["request_json"],
    )


async def test_spa_jobs_from_captured_api_responses() -> None:
    """Ahamove: HTML là khung SPA, trình duyệt gọi API tin tuyển dụng; tin lấy từ JSON."""
    url = "https://careers.ahamove.com/jobs"
    shell = (
        '<html><body><div id="root"></div><script src="/assets/index.js"></script></body></html>'
    )
    fetcher = FakeFetcher()
    fetcher.add(url, shell)
    links = "".join(
        f'<a href="/jobs/{slug}">x</a>'
        for slug in ("ctv-kinh-doanh-3", "senior-accounts-receivable-executive")
    )
    fetcher.add_rendered(
        url,
        f"<html><body><main><h1>Việc làm</h1>{links}</main></body></html>",
        captured=[captured("ahamove_job_fields.json"), captured("ahamove_jobs.json")],
    )
    s = session(fetcher)
    jobs = await run(CareersSource(url), s)
    by_title = {j.title: j for j in jobs}
    assert len(by_title) == 7
    assert by_title["CTV Kinh Doanh"].url == f"{url}/ctv-kinh-doanh-3"
    assert all(len(j.description or "") >= 100 for j in jobs)
    # Đã có đủ mô tả trong JSON: không mở trang chi tiết.
    detail = [u for kind, u, _ in fetcher.calls if u.startswith(f"{url}/")]
    assert detail == []
    render = [kw for kind, _, kw in fetcher.calls if kind == "render"]
    assert render[0]["capture_json"] is True


def api_page(page: int, *, size: int = 2, total: int = 5) -> dict[str, Any]:
    first = (page - 1) * size
    return {
        "total": total,
        "items": [
            {
                "id": n,
                "title": f"Vị trí số {n}",
                "location": "Hà Nội",
                "description": "Mô tả công việc chi tiết. " * 10,
            }
            for n in range(first, min(first + size, total))
        ],
    }


def spa_site(api: str, pages: dict[int, Any]) -> FakeFetcher:
    """Trang careers SPA: khung HTML, khi render gọi `api` trang 1; các trang sau qua HTTP."""
    listing = "https://hr.a.vn/viec-lam"
    fetcher = FakeFetcher()
    fetcher.add(listing, '<html><body><div id="app"></div></body></html>')
    first = CapturedJson(
        url=f"{api}?pageSize=2&page=1",
        method="GET",
        status=200,
        body=json.dumps(pages[1]).encode(),
    )
    fetcher.add_rendered(
        listing, "<html><body><div id='app'>…</div></body></html>", captured=[first]
    )
    for page, body in pages.items():
        if page > 1:
            fetcher.add(
                f"{api}?pageSize=2&page={page}", json.dumps(body), content_type="application/json"
            )
    return fetcher


async def test_spa_api_is_read_page_by_page() -> None:
    api = "https://api.hr.a.vn/jobs"
    fetcher = spa_site(api, {1: api_page(1), 2: api_page(2), 3: api_page(3)})
    s = session(fetcher)
    jobs = await run(CareersSource("https://hr.a.vn/viec-lam"), s)
    assert [j.title for j in jobs] == [f"Vị trí số {n}" for n in range(5)]
    assert s.truncated == []
    api_calls = [u for kind, u, _ in fetcher.calls if u.startswith(api)]
    assert api_calls == [f"{api}?pageSize=2&page=2", f"{api}?pageSize=2&page=3"]  # đủ total: dừng


async def test_spa_api_stops_on_repeat_cap_or_block() -> None:
    api = "https://api.hr.a.vn/jobs"
    # API bỏ qua tham số trang (trả lại trang 1): dừng ngay, không đọc tới giới hạn.
    repeat = spa_site(api, {1: api_page(1), 2: api_page(1), 3: api_page(1)})
    jobs = await run(CareersSource("https://hr.a.vn/viec-lam"), session(repeat))
    assert len(jobs) == 2
    assert [u for _, u, _ in repeat.calls if u.startswith(api)] == [f"{api}?pageSize=2&page=2"]

    # Chạm giới hạn trang API: nguồn chưa xong.
    many = spa_site(api, {n: api_page(n, total=20) for n in range(1, 11)})
    capped = session(many, max_api_pages=3)
    jobs = await run(CareersSource("https://hr.a.vn/viec-lam"), capped)
    assert len(jobs) == 6
    assert capped.truncated == ["api_pages"]

    # API trang 2 bị chặn: giữ tin trang 1, nguồn vẫn chạy tiếp.
    blocked = spa_site(api, {1: api_page(1)})
    blocked.add(f"{api}?pageSize=2&page=2", "captcha", status=403, outcome=FetchOutcome.BLOCKED)
    s = session(blocked)
    jobs = await run(CareersSource("https://hr.a.vn/viec-lam"), s)
    assert len(jobs) == 2
    assert s.truncated == ["api_pages"]


async def test_jobs_embedded_in_next_data() -> None:
    """VNG: danh sách tin chỉ có trong `__NEXT_DATA__` (HTML không có link tin)."""
    url = "https://career.vng.com.vn/vi/tim-kiem-viec-lam"
    fetcher = FakeFetcher()
    fetcher.add(url, html("json/vng_search.html"))
    # Trang 2 (`?page=2` trên chính trang, Next.js trả JSON trang 2); trang 3 trả lại trang 2:
    # dừng.
    page2 = (
        html("json/vng_search.html")
        .replace('"job_id":', '"job_id":1')
        .replace("Technical Support Engineer", "Kỹ sư hỗ trợ kỹ thuật")
    )
    fetcher.add(f"{url}?page=2", page2)
    fetcher.add(f"{url}?page=3", page2)
    jobs = await run(CareersSource(url), session(fetcher, max_rendered_pages=0))
    assert len(jobs) == 20
    assert jobs[0].title == "Technical Support Engineer, GreenNode"
    assert jobs[10].title == "Kỹ sư hỗ trợ kỹ thuật, GreenNode"
    assert all(len(j.description or "") >= 100 for j in jobs)


async def test_thin_detail_page_is_rendered() -> None:
    """Tiki: HTML trang tin chỉ có tiêu đề + hạn nộp, mô tả do JavaScript nạp."""
    listing = "https://tuyendung.tiki.vn/"
    job = "https://tuyendung.tiki.vn/job_detail?request_id=TIKI_Business_01102026_410"
    fetcher = FakeFetcher()
    fetcher.add(
        listing,
        "<main><h1>Tuyển dụng</h1>"
        + "<p>Gia nhập Tiki. </p>" * 20
        + f'<a href="{job}">Key Account Sales</a></main>',
    )
    thin = (
        "<main><h1>Key Account Sales</h1><p>Business</p><p>Hồ Chí Minh</p><p>Full-time</p>"
        "<h6>Application Deadline: 07/10/2026 - 31/10/2026</h6>"
        + "<p>Ứng tuyển ngay để trở thành một phần của đội ngũ.</p>" * 4
        + "</main>"
    )
    fetcher.add(job, thin)
    fetcher.add_rendered(job, JD.format(title="Key Account Sales"))
    jobs = await run(CareersSource(listing), session(fetcher))
    assert [j.title for j in jobs] == ["Key Account Sales"]
    assert "Phát triển tính năng mới" in (jobs[0].description or "")
    assert ("render", job) in [(kind, u) for kind, u, _ in fetcher.calls]


def test_pick_title_prefers_heading_matching_listing_text() -> None:
    banner = "TMA tự hào là nơi làm việc tin cậy của"
    assert pick_title("System Test Engineer", [banner, "System Test Engineer (HCM)"], None) == (
        "System Test Engineer (HCM)"
    )
    assert pick_title("Product Owner", [banner], "TMA Solutions") == "Product Owner"
    assert pick_title(None, ["Kế toán trưởng"], "OG") == "Kế toán trưởng"
    assert pick_title(None, [], "OG") == "OG"


def test_pick_title_from_card_text_and_url_slug() -> None:
    # MISA: chữ trên thẻ tin gồm cả nhãn, lương, nơi làm, hạn nộp; <h1> là tên vị trí.
    card = "NEW Nhân viên Tester/QC Thỏa thuận Thỏa thuận Hà Nội Hà Nội Hạn nộp hồ sơ: 31/10/2026"
    assert pick_title(card, ["Nhân viên Tester/QC"], "Nhân viên Tester/QC") == (
        "Nhân viên Tester/QC"
    )
    assert pick_title(card, ["Tuyển dụng"], None) == "Nhân viên Tester/QC Hà Nội Hà Nội"
    # TMA: <h1> là khẩu hiệu, link không có chữ; tên vị trí ở <h5>, khớp chữ trong URL.
    banner = "TMA tự hào là nơi làm việc tin cậy của"
    url = "https://www.tma.vn/tuyen-dung/chi-tiet/tester-1061"
    assert slug_hint(url) == "tester"
    assert pick_title(None, [banner], "Chi tiết", extra=["Liên hệ", "Tester"], slug="tester") == (
        "Tester"
    )
    # Không gì khớp chữ trong URL: như cũ.
    assert pick_title(None, ["Kế toán"], None, slug="job") == "Kế toán"
    assert slug_hint("https://a.vn/jobs/58434444/") is None
    assert slug_hint("https://a.vn/viec-lam/ke-toan-tong-hop-123.html") == "ke toan tong hop"
    assert clean_card_text("HOT – Kế toán | Cạnh tranh") == "Kế toán"


@pytest.mark.parametrize(
    ("segment", "stop"),
    [
        ("privacy-policy", True),
        ("chinh-sach-bao-mat", True),
        ("cookie-policy", True),
        ("terms-of-use", True),
        ("tim-kiem-theo-hash-tag", True),
        ("search", True),
        ("saved-jobs", True),
        # Tên vị trí có chữ trùng từ khoá của trang chính sách / tìm kiếm.
        ("chuyen-vien-bao-mat-thong-tin-12", False),
        ("chuyen-vien-bao-mat-thong-tin", False),
        ("ky-su-bao-mat", False),
        ("chuyen-vien-chinh-sach-tin-dung-45", False),
        ("search-engine-optimization-specialist", False),
        ("ky-su-loc-hoa-dau", False),
        ("loc-hoa-dau-nghi-son-ky-su-van-hanh", False),
    ],
)
def test_stop_segments(segment: str, stop: bool) -> None:
    assert _stop(segment) is stop


def test_policy_pages_and_locale_root_are_not_jobs() -> None:
    links = [
        Link("https://www.grab.careers/en/privacy-policy/", "Recruitment Privacy Policy"),
        Link("https://www.grab.careers/en/jobs/744000153509469/associate-bd/", "Associate, BD"),
        Link("https://www.grab.careers/en/life-at-grab/", "Life at Grab"),
    ]
    assert [j.url for j in job_links(links, "https://www.grab.careers/en/")] == [
        "https://www.grab.careers/en/jobs/744000153509469/associate-bd/"
    ]


def test_listing_index_links_are_followed() -> None:
    """Trang gốc careers chỉ trỏ tới trang mục lục việc làm (VNG, Grab, SuccessFactors)."""
    vng = [
        Link("https://career.vng.com.vn/vi/tim-kiem-viec-lam", "Tìm công việc"),
        Link("https://career.vng.com.vn/vi/su-kien", "Sự kiện"),
    ]
    assert listing_links(vng, "https://career.vng.com.vn/") == [
        "https://career.vng.com.vn/vi/tim-kiem-viec-lam"
    ]
    grab = [
        Link("https://www.grab.careers/en/jobs/", "Jobs"),
        Link("https://www.grab.careers/en/jobs/saved-jobs/", "Saved Jobs"),
        Link("https://www.grab.careers/en/privacy-policy/", "View all jobs"),
    ]
    assert listing_links(grab, "https://www.grab.careers/en/") == [
        "https://www.grab.careers/en/jobs/"
    ]
    vcb = [
        Link("https://tuyendung.vcb.vn/search/", "Tìm việc"),
        Link("https://tuyendung.vcb.vn/search/?q=ke+toan", "Kế toán"),
    ]
    assert listing_links(vcb, "https://tuyendung.vcb.vn/") == ["https://tuyendung.vcb.vn/search/"]


def test_split_markdown() -> None:
    md = "\n".join(f"### Vị trí {i}\nMô tả ngắn" for i in range(100))
    chunks = split_markdown(md, 300)
    assert all(len(c) <= 300 for c in chunks)
    assert "\n".join(chunks) == md
    assert split_markdown("x" * 50, 20) == ["x" * 20]  # dòng quá dài bị cắt


async def test_llm_listing_reads_long_pages_in_chunks() -> None:
    url = "https://careers.shop.vn/jobs"
    cards = "".join(
        f"<div><h3>Vị trí số {i}</h3><p>{'Mô tả công việc. ' * 20}</p></div>" for i in range(80)
    )
    fetcher = FakeFetcher()
    fetcher.add(url, f"<html><body><main>{cards}</main></body></html>")
    replies = [
        {"is_job_listing": True, "jobs": [{"title": f"Vị trí số {i}"}, {"title": "Vị trí số 0"}]}
        for i in range(4)
    ]
    llm, chat = fake_llm(*replies)
    jobs = await run(CareersSource(url), session(fetcher, llm, max_rendered_pages=0))
    assert len(chat.calls) == 4  # ~28 nghìn ký tự → 5 đoạn, chỉ đọc 4
    assert [j.title for j in jobs] == [f"Vị trí số {i}" for i in range(4)]  # gộp trùng tên
