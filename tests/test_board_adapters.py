"""Adapter từng trang tuyển dụng trên mẫu thật (tests/fixtures/boards/<trang>/).

Dòng đầu mỗi file HTML mẫu là `<!-- URL gốc -->`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from jobcrawler.fakes import FakeChatClient, FakeFetcher, fetch_result
from jobcrawler.llm import LLMClient
from jobcrawler.net.http import FetchResult
from jobcrawler.schemas import ExtractionMethod
from jobcrawler.sources import boards
from jobcrawler.sources.base import SourceSession
from jobcrawler.sources.boards.base import (
    BoardAdapter,
    BoardEntry,
    ListingState,
    SitemapBoard,
    parse_sitemap,
)
from jobcrawler.sources.boards.vietnamworks import API as VNW_API
from jobcrawler.sources.boards.vietnamworks import find_object, flight_payload

FIXTURES = Path(__file__).parent / "fixtures" / "boards"
REGISTRY = boards.registry()


def page(board: str, name: str) -> tuple[str, str]:
    html = (FIXTURES / board / name).read_text(encoding="utf-8")
    first = html.splitlines()[0].strip()
    assert first.startswith("<!--"), f"{board}/{name} thiếu dòng URL"
    return html, first.removeprefix("<!--").removesuffix("-->").strip()


def session(fetcher: FakeFetcher) -> SourceSession:
    return SourceSession("board:test", fetcher, LLMClient(FakeChatClient(["{}"]), model=None))


async def entries(
    adapter: BoardAdapter, fetcher: FakeFetcher
) -> tuple[list[BoardEntry], ListingState]:
    state = ListingState()
    return [e async for e in adapter.entries(session(fetcher), state)], state


def test_registry_order_and_names() -> None:
    assert list(REGISTRY) == [
        "topcv",
        "careerlink",
        "jobsgo",
        "careerviet",
        "vietnamworks",
        "glints",
        "topdev",
        "vieclam24h",
        "itviec",
        "123job",
    ]
    assert all(a.enabled for a in REGISTRY.values())


# --- trang chi tiết tin -------------------------------------------------------------------------

JOBS: list[tuple[str, str, dict[str, Any]]] = [
    (
        "topcv",
        "job_1.html",
        {
            "external_id": "2333688",
            "title": "Chuyên Viên Tài Chính Nguồn Vốn (Tín Dụng DN)",
            "salary": (20_000_000, 22_000_000, "VND"),
            "profile": (
                "225567",
                "https://www.topcv.vn/cong-ty/cong-ty-co-phan-cloud-property/225567.html",
            ),
        },
    ),
    (
        "topcv",
        "job_2.html",
        {"external_id": "2330625", "salary_raw": "Thoả thuận", "profile": ("3", None)},
    ),
    (
        "topcv",
        "job_3.html",
        {
            "external_id": "1752022",
            "profile": ("219468", "https://www.topcv.vn/brand/ipa?id=219468"),
        },
    ),
    (
        "careerviet",
        "job_1.html",
        {
            "external_id": "35C8AEEB",
            "company": "Ngân Hàng TMCP Quân Đội",
            "salary": (9_000_000, 55_000_000, "VND"),
            "employment_type": "FULL_TIME",
            "profile": ("35A66D70", None),
            "website": "https://careers.mbbank.com.vn/",
        },
    ),
    ("careerviet", "job_2.html", {"external_id": "35C88F65", "salary_raw": "Cạnh tranh"}),
    (
        "careerlink",
        "job_1.html",
        {
            "external_id": "3652914",
            "company": "CÔNG TY CỔ PHẦN LOTHAMILK",
            "salary": (8_000_000, 11_000_000, "VND"),
            "profile": (
                "196578",
                "https://www.careerlink.vn/viec-lam-cua/cong-ty-co-phan-lothamilk/196578",
            ),
        },
    ),
    ("careerlink", "job_2.html", {"external_id": "3640088", "profile": ("19032", None)}),
    (
        "jobsgo",
        "job_1.html",
        {
            "external_id": "29157059643",
            "salary": (15_000_000, 30_000_000, "VND"),
            "profile": (
                "37532356",
                "https://jobsgo.vn/brand/cong-ty-co-phan-cong-nghe-kiotviet-37532356.html",
            ),
        },
    ),
    ("jobsgo", "job_2.html", {"external_id": "29378438080", "profile": ("1892478072", None)}),
    ("jobsgo", "job_3.html", {"external_id": "29377541866", "profile": ("1503168142", None)}),
    (
        "vietnamworks",
        "job_2_http.html",
        {
            "external_id": "2111825",
            "title": "Phiên Dịch Tiếng Hàn",
            "company": "VINFAST TRADING AND PRODUCTION JSC",
            "salary_raw": "Thương lượng",
            "method": ExtractionMethod.PARSER,
            "profile": ("273080", None),
        },
    ),
    (
        "vietnamworks",
        "job_1.html",
        {"external_id": "2112837", "salary_raw": "40tr-60tr ₫/tháng", "profile": ("408938", None)},
    ),
    (
        "glints",
        "job_1.html",
        {
            "external_id": "7f1c0e92-3a4d-46f5-8676-f21acf6b13fe",
            "company": "Viettel Cyber Security",
            "profile": ("7d4ed919-9af9-44a8-8645-86fc53d048fd", None),
            "website": "https://viettelcybersecurity.com/",
        },
    ),
    (
        "topdev",
        "job_1.html",
        {
            "external_id": "2137774",
            "title": "Chuyên viên đảm bảo chất lượng (QA/QC)",
            "employment_type": None,  # "OTHER" bị bỏ
            "profile": (
                "94320",
                "https://topdev.vn/nha-tuyen-dung/tong-cong-ty-ha-tang-mang-vnpt-net-94320",
            ),
        },
    ),
    ("topdev", "job_2.html", {"external_id": "2136432", "profile": ("86167", None)}),
    (
        "vieclam24h",
        "job_1.html",
        {"external_id": "200958285", "salary_raw": "Thỏa thuận", "profile": ("4082455", None)},
    ),
    ("vieclam24h", "job_3.html", {"external_id": "200871489", "profile": ("5548557", None)}),
    (
        "itviec",
        "job_1.html",
        {
            "external_id": "ky-su-giai-phap-cloud-viettel-group-3452",
            "title": "Kỹ sư Giải pháp Cloud",  # từ <h1>: JSON-LD đã bỏ dấu
            "salary": (1000, 2000, "USD"),
            "profile": ("viettel-group", "https://itviec.com/nha-tuyen-dung/viettel-group"),
        },
    ),
    ("itviec", "job_3.html", {"external_id": "business-analyst-erp-logistics-wms-cellphones-2214"}),
    (
        "123job",
        "job_1.html",
        {
            "external_id": "v91BPO1LrW",
            "salary": (8_000_000, 12_000_000, "VND"),
            "profile": ("94zGpQOnR1", None),
        },
    ),
    ("123job", "job_3.html", {"external_id": "Oq5MaON1qp", "employment_type": None}),
]


@pytest.mark.parametrize(("board", "name", "want"), JOBS, ids=[f"{b}/{n}" for b, n, _ in JOBS])
def test_parse_job(board: str, name: str, want: dict[str, Any]) -> None:
    adapter = REGISTRY[board]
    html, url = page(board, name)
    parsed = adapter.parse_job(html, url)
    assert parsed is not None
    job = parsed.job
    assert job.source == board
    assert job.external_id == want["external_id"] == adapter.job_id(url)
    assert job.url == adapter.canonical(url)
    assert job.title
    assert job.description
    assert len(job.description) > 200
    assert job.method is want.get("method", ExtractionMethod.JSONLD)
    # `identifier` thường là mã công ty: chỉ giữ ở trang ghi mã tin ở đó.
    assert ("identifier" in job.extra) == adapter.identifier_is_job
    if "title" in want:
        assert job.title.strip() == want["title"]
    if "company" in want:
        assert job.company_name == want["company"]
    if "salary" in want:
        assert (job.salary_min, job.salary_max, job.salary_currency) == want["salary"]
    if "salary_raw" in want:
        assert job.salary_raw == want["salary_raw"]
    if "employment_type" in want:
        assert job.employment_type == want["employment_type"]
    if "profile" in want:
        assert parsed.profile is not None
        external_id, profile_url = want["profile"]
        assert parsed.profile.external_id == external_id
        assert adapter.profile_id(parsed.profile.url) == external_id
        if profile_url:
            assert parsed.profile.url == profile_url
        assert parsed.profile.name
    if "website" in want:
        assert parsed.profile is not None
        assert parsed.profile.website == want["website"]


def test_vieclam24h_reads_employer_website_and_tax_code_from_page_data() -> None:
    """Website + MST của nhà tuyển dụng nằm trong `__NEXT_DATA__` của trang tin."""
    html, url = page("vieclam24h", "job_1.html")
    employer = json.loads(
        (FIXTURES / "vieclam24h" / "job_1.next_data.json").read_text(encoding="utf-8")
    )
    data = {
        "props": {
            "initialState": {
                "api": {
                    "employerDetailHiddenContact": {"data": employer["employerDetailHiddenContact"]}
                }
            }
        }
    }
    # Mẫu đã bỏ nội dung script: điền lại `__NEXT_DATA__` (phần nhà tuyển dụng của trang thật).
    script = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'
    html = re.sub(r'<script id="__NEXT_DATA__"[^>]*></script>', lambda _: script, html)
    assert script in html

    parsed = REGISTRY["vieclam24h"].parse_job(html, url)
    assert parsed is not None
    assert parsed.profile is not None
    assert parsed.profile.tax_code == "0100233583"
    assert parsed.profile.website == "https://tuyendung.vpbank.com.vn/vi"


def test_vietnamworks_payload_skips_contact_fields() -> None:
    html, url = page("vietnamworks", "job_2_http.html")
    obj = find_object(flight_payload(html), "jobId", 2111825, require="jobTitle")
    assert obj is not None
    assert "emailAddress" in obj  # có trong payload...
    parsed = REGISTRY["vietnamworks"].parse_job(html, url)
    assert parsed is not None
    dumped = parsed.job.model_dump_json()
    assert "recruiter@example.com" not in dumped  # ...nhưng không vào tin
    assert parsed.job.expires_raw == "2026-10-25T23:59:59+07:00"
    assert parsed.job.requirements


# --- trang profile ------------------------------------------------------------------------------

PROFILES: list[tuple[str, str, dict[str, Any]]] = [
    (
        "topcv",
        "company_1.html",
        {"id": "3", "name": "FPT Software", "website": "https://www.fpt-software.com/"},
    ),
    (
        "careerviet",
        "company_1_http.html",
        {"id": "35A66D70", "website": "https://careers.mbbank.com.vn/", "tax_code": "0100283873"},
    ),
    (
        "careerlink",
        "company_1.html",
        {"id": "19032", "name": "FE CREDIT", "website": "http://www.fecredit.com.vn/"},
    ),
    ("jobsgo", "company_1.html", {"id": "1503168142", "tax_code": "0107593793"}),
    ("jobsgo", "company_2.html", {"id": "37532356", "name": "Công Ty Cổ Phần Công Nghệ Kiotviet"}),
    (
        "vietnamworks",
        "company_2.html",
        {"id": "408938", "name": "Công Ty Cổ Phần Đầu Tư Và Xây Dựng SGC"},
    ),
    (
        "glints",
        "company_1.html",
        {
            "id": "7d4ed919-9af9-44a8-8645-86fc53d048fd",
            "website": "https://viettelcybersecurity.com/",
        },
    ),
    ("topdev", "company_1.html", {"id": "94320", "website": "https://vnptnet.vn/"}),
    (
        "vieclam24h",
        "company_1.html",
        {"id": "4082455", "name": "Ngân Hàng TMCP Việt Nam Thịnh Vượng - VPBank"},
    ),
    (
        "itviec",
        "company_1.html",
        {
            "id": "cellphones",
            "name": "CellphoneS",
            "website": "https://tuyendung.cellphones.com.vn/",
        },
    ),
    (
        "123job",
        "company_1.html",
        {
            "id": "y3zRVVb1z2",
            "name": "Công ty cổ phần thương mại và dịch vụ emall",
            "website": None,
        },
    ),
]


@pytest.mark.parametrize(
    ("board", "name", "want"), PROFILES, ids=[f"{b}/{n}" for b, n, _ in PROFILES]
)
def test_parse_profile(board: str, name: str, want: dict[str, Any]) -> None:
    html, url = page(board, name)
    profile = REGISTRY[board].parse_profile(html, url)
    assert profile is not None
    assert profile.external_id == want["id"]
    assert profile.name
    for key in ("name", "website", "tax_code"):
        if key in want:
            assert getattr(profile, key) == want[key]


def test_careerlink_placeholder_employer_is_ignored() -> None:
    html, _url = page("careerlink", "company_1.html")
    fake = html.replace("/viec-lam-cua/fe-credit/19032", "/viec-lam-cua/careerlink-s-client/3470")
    assert (
        REGISTRY["careerlink"].parse_profile(
            fake, "https://www.careerlink.vn/viec-lam-cua/careerlink-s-client/3470"
        )
        is None
    )


@pytest.mark.parametrize(
    ("board", "name", "jobs_url", "min_jobs"),
    [
        (
            "topcv",
            "company_1_jobs.html",
            "https://www.topcv.vn/cong-ty/fpt-software-cid3/tuyen-dung.html",
            10,
        ),
        ("careerviet", "company_1.html", None, 5),
        ("careerlink", "company_1.html", None, 10),
        ("jobsgo", "company_1.html", None, 5),
        ("glints", "company_1.html", None, 1),
        ("vieclam24h", "company_1.html", None, 5),
        ("itviec", "company_1.html", None, 2),
    ],
)
def test_profile_job_urls(board: str, name: str, jobs_url: str | None, min_jobs: int) -> None:
    adapter = REGISTRY[board]
    html, url = page(board, name)
    urls = adapter.profile_job_urls(html, url)
    assert len(urls) >= min_jobs
    assert all(adapter.job_id(u) for u in urls)
    assert all(u == adapter.canonical(u) for u in urls)  # không còn ?ref=, ?lab_feature=...
    if jobs_url:
        assert (
            adapter.profile_jobs_url("https://www.topcv.vn/cong-ty/fpt-software/3.html") == jobs_url
        )


def test_topcv_brand_profile_jobs_url() -> None:
    topcv = REGISTRY["topcv"]
    assert topcv.profile_jobs_url("https://www.topcv.vn/brand/ipa?id=219468") == (
        "https://www.topcv.vn/brand/ipa/tuyen-dung?id=219468"
    )
    assert topcv.profile_id("https://www.topcv.vn/brand/ipa?id=219468&utm_source=x") == "219468"


# --- danh sách tin ------------------------------------------------------------------------------


def _sitemap_site(board: str) -> FakeFetcher:
    """Phục vụ sitemap mẫu theo đúng URL trong index: index → (index tin) → sitemap tin."""
    adapter = REGISTRY[board]
    assert isinstance(adapter, SitemapBoard)
    folder = FIXTURES / board
    fetcher = FakeFetcher()
    index = (folder / "sitemap_index.xml").read_text(encoding="utf-8")
    fetcher.add(adapter.sitemaps[0], index, content_type="application/xml")
    child = next(u for u, _ in parse_sitemap(index)[1] if adapter.job_sitemap.search(u))
    jobs = (folder / "sitemap_jobs.xml").read_text(encoding="utf-8")
    if (folder / "sitemap_jobs_index.xml").exists():
        nested = (folder / "sitemap_jobs_index.xml").read_text(encoding="utf-8")
        fetcher.add(child, nested, content_type="application/xml")
        child = next(
            u for u, _ in parse_sitemap(nested)[1] if adapter.job_sitemap.search(u) and u != child
        )
    fetcher.add(child, jobs, content_type="application/xml")
    return fetcher


@pytest.mark.parametrize("board", ["topcv", "jobsgo", "careerviet", "glints", "topdev", "itviec"])
async def test_sitemap_entries(board: str) -> None:
    adapter = REGISTRY[board]
    assert isinstance(adapter, SitemapBoard)
    found, _ = await entries(adapter, _sitemap_site(board))
    assert len(found) >= 3
    assert len({e.external_id for e in found}) == len(found)
    for entry in found:
        assert entry.url == adapter.canonical(entry.url)
        assert adapter.job_id(entry.url) == entry.external_id
        if not adapter.trust_lastmod:
            assert entry.lastmod is None
    if board in ("glints", "careerviet", "topdev"):
        assert not any("/en/" in e.url or "detail-jobs" in e.url for e in found)


async def test_careerlink_listing_pages() -> None:
    adapter = REGISTRY["careerlink"]
    html, _ = page("careerlink", "listing_1.html")
    fetcher = FakeFetcher()
    fetcher.add(adapter.listing_url.format(page=1), html)  # pyright: ignore[reportAttributeAccessIssue]
    fetcher.add(adapter.listing_url.format(page=2), html)  # pyright: ignore[reportAttributeAccessIssue]
    found, state = await entries(adapter, fetcher)
    assert len(found) == 50
    assert state.complete  # trang 2 không có tin mới → hết danh sách
    assert [u for _, u, _ in fetcher.calls][-1].endswith("page=2")


async def test_partial_listing_boards_are_never_complete() -> None:
    adapter = REGISTRY["123job"]
    fetcher = FakeFetcher()
    fetcher.add_file("https://123job.vn/rss/index", FIXTURES / "123job" / "rss_jobs.xml")
    found, state = await entries(adapter, fetcher)  # trang danh sách 404: chỉ có RSS
    assert len(found) == 10
    assert all(adapter.job_id(e.url) == e.external_id for e in found)
    assert not state.complete


async def test_vietnamworks_api_entries() -> None:
    adapter = REGISTRY["vietnamworks"]
    data = json.loads((FIXTURES / "vietnamworks" / "search_api.json").read_text(encoding="utf-8"))
    bodies: list[dict[str, Any]] = []

    def api(kwargs: dict[str, Any]) -> FetchResult:
        bodies.append(kwargs["json"])
        reply = (
            dict(data, meta={**data["meta"], "nbPages": 2})
            if kwargs["json"]["page"] == 0
            else {**data, "data": []}
        )
        return fetch_result(VNW_API, json.dumps(reply), content_type="application/json")

    fetcher = FakeFetcher()
    fetcher.add_route(VNW_API, api)
    found, state = await entries(adapter, fetcher)
    assert [e.external_id for e in found] == ["2120659", "2120657"]
    assert found[0].lastmod is not None
    assert state.complete
    assert [b["page"] for b in bodies] == [0, 1]
    assert "emailAddress" not in bodies[0]["retrieveFields"]


def test_vietnamworks_payload_with_row_references() -> None:
    """Trang thật: mô tả/yêu cầu là tham chiếu tới dòng văn bản RSC, mảng địa điểm/kỹ năng là
    tham chiếu tới dòng JSON; bản tóm tắt (mô tả cắt ~300 ký tự) xuất hiện trước bản đầy đủ."""
    html, url = page("vietnamworks", "job_3_http_refs.html")
    parsed = REGISTRY["vietnamworks"].parse_job(html, url)
    assert parsed is not None
    job = parsed.job
    assert job.method is ExtractionMethod.PARSER
    assert job.description
    assert len(job.description) > 1000
    assert job.requirements
    assert len(job.requirements) > 1000
    assert job.benefits
    assert job.benefits.startswith("<ul><li>Bonus")
    assert len(job.locations) == 2
    assert "Autocad" in job.skills
    assert (job.salary_min, job.salary_max) == (20_000_000, 30_000_000)
    assert "recruiter@example.com" not in job.model_dump_json()
    assert parsed.profile is not None
    assert parsed.profile.external_id == "283527"
