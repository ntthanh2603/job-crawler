from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from jobcrawler.common.schemas import ExtractionMethod, FetchOutcome, RawJob
from jobcrawler.services.fakes import FakeChatClient, FakeFetcher, fetch_result
from jobcrawler.services.llm import LLMClient
from jobcrawler.services.net.http import FetchResult
from jobcrawler.sources import ats
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
from jobcrawler.sources.base import KnownJobs, Source, SourceError, SourceLimits, SourceSession

FIXTURES = Path(__file__).parent / "fixtures"


def load(path: str) -> Any:
    return json.loads((FIXTURES / path).read_text(encoding="utf-8"))


def session(fetcher: FakeFetcher, **limits: Any) -> SourceSession:
    llm = LLMClient(FakeChatClient(["{}"]), model=None)  # LLM tắt
    return SourceSession("test", fetcher, llm, SourceLimits(**limits))


async def collect(source: Source, s: SourceSession) -> list[RawJob]:
    return [job async for job in source.jobs(s)]


def ref_of(url: str) -> ats.AtsRef:
    ref = ats.match_url(url)
    assert ref is not None
    return ref


# --- nhận diện --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "name"),
    [
        ("https://job-boards.greenhouse.io/gitlab/jobs/8860302002", "greenhouse:gitlab"),
        ("https://boards.greenhouse.io/embed/job_board?for=Cake&b=https://x", "greenhouse:cake"),
        ("https://boards.greenhouse.io/embed/job_app?for=acme&token=1", "greenhouse:acme"),
        ("https://boards-api.greenhouse.io/v1/boards/xyz/jobs", "greenhouse:xyz"),
        ("https://jobs.lever.co/palantir/6ed76ce8-4156", "lever:palantir"),
        ("https://api.lever.co/v0/postings/acme?mode=json", "lever:acme"),
        ("https://jobs.ashbyhq.com/openai/8fb1615c", "ashby:openai"),
        ("https://jobs.ashbyhq.com/Acme%20Corp", "ashby:acme corp"),
        ("https://apply.workable.com/huggingface/j/81B46579FE/", "workable:huggingface"),
        ("https://acme.workable.com/", "workable:acme"),
        ("https://intel.wd1.myworkdayjobs.com/en-US/External/job/x_JR1", "workday:intel/external"),
        ("https://nab.wd3.myworkdayjobs.com/nab_careers", "workday:nab/nab_careers"),
        ("https://seek.bamboohr.com/careers/15", "bamboohr:seek"),
        ("https://bunq.recruitee.com/o/head-of-product", "recruitee:bunq"),
        ("https://career.teamtailor.com/jobs/8118064-x", "teamtailor:career.teamtailor.com"),
        ("https://careers.smartrecruiters.com/BoschGroup", "smartrecruiters:boschgroup"),
    ],
)
def test_match_url(url: str, name: str) -> None:
    ref = ats.match_url(url)
    assert ref is not None
    assert ref.name == name


@pytest.mark.parametrize(
    "url",
    [
        "https://www.bamboohr.com/careers/",
        "https://apply.workable.com/j/81B46579FE",
        "https://boards.greenhouse.io/embed",
        "https://www.teamtailor.com/en/",
        "https://example.com/careers",
    ],
)
def test_match_url_ignores_vendor_pages(url: str) -> None:
    assert ats.match_url(url) is None


def test_detect_in_html() -> None:
    html = """
    <a href="https://jobs.lever.co/acme">Xem việc làm</a>
    <script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>
    <script>var cfg = {"board":"https:\\/\\/jobs.ashbyhq.com\\/acme"};</script>
    <iframe src="//acme.recruitee.com/embed"></iframe>
    <a href="https://jobs.lever.co/acme/123">trùng</a>
    <a href="https://careers.smartrecruiters.com/Acme">SR</a>
    """
    refs = ats.detect(html, "https://acme.vn/careers")
    assert [r.name for r in refs] == [
        "lever:acme",
        "greenhouse:acme",
        "ashby:acme",
        "recruitee:acme",
        "smartrecruiters:acme",
    ]
    assert [ats.supported(r) for r in refs] == [True, True, True, True, False]
    assert ats.source_for(refs[-1]) is None
    assert "robots.txt" in (ats.unsupported_reason(refs[-1]) or "")


def test_teamtailor_custom_domain() -> None:
    html = (
        '<link rel="stylesheet" href="https://scripts.teamtailor-cdn.com/x.css">'
        '<link rel="alternate" type="application/rss+xml" title="Jobs" '
        'href="https://careers.acme.vn/jobs.rss" />'
    )
    [ref] = ats.detect(html, "https://careers.acme.vn/")
    assert ref.name == "teamtailor:careers.acme.vn"
    assert ref.params["rss"] == "https://careers.acme.vn/jobs.rss"


# --- parser trên mẫu thật --------------------------------------------------------------------


def test_greenhouse_parse() -> None:
    jobs = greenhouse.parse_jobs(load("greenhouse/gitlab_jobs.json"), "gitlab")
    assert len(jobs) == 3
    job = jobs[0]
    assert job.external_id == "gitlab:8860302002"
    assert job.title == "Account Executive - France"
    assert job.company_name == "GitLab"
    assert job.url == "https://job-boards.greenhouse.io/gitlab/jobs/8860302002"
    assert job.description
    assert job.description.startswith("<div")
    assert job.locations == ["Remote, France"]
    assert job.department == "EMEA - Commercial"
    assert job.posted_at == datetime(2026, 10, 2, 15, 31, 50, tzinfo=UTC)
    assert job.method is ExtractionMethod.ATS_API
    assert jobs[2].locations == [
        "Remote, Canada",
        "Remote, United Kingdom",
        "Remote, United States",
    ]


def test_lever_parse() -> None:
    jobs = lever.parse_postings(load("lever/palantir_postings.json"), "palantir")
    job = jobs[0]
    assert job.external_id == "palantir:6ed76ce8-4156-4b60-b120-403538bd66cd"
    assert job.title == "Administrative Business Partner"
    assert job.locations == ["Singapore, Singapore"]
    assert (job.work_mode, job.employment_type, job.department) == (
        "hybrid",
        "Full-time",
        "Administrative",
    )
    assert job.apply_url
    assert job.apply_url.endswith("/apply")
    assert job.description
    assert job.description.count("<h3>") == 3
    assert job.posted_at is not None
    assert job.posted_at.year == 2026


def test_ashby_parse() -> None:
    jobs = ashby.parse_job_board(load("ashby/openai_job_board.json"), "openai")
    assert [j.employment_type for j in jobs] == ["FullTime", "FullTime", "Contract"]
    first, remote, contract = jobs
    assert (first.salary_min, first.salary_max, first.salary_currency, first.salary_period) == (
        257000,
        335000,
        "USD",
        "1 YEAR",
    )
    assert first.salary_raw == "$257K - $335K"
    assert remote.work_mode == "Remote"
    assert remote.locations == ["US - Remote", "Seattle", "San Francisco"]
    assert contract.salary_period == "1 HOUR"


def test_ashby_skips_unlisted() -> None:
    data = {"jobs": [{"id": "1", "title": "Ẩn", "isListed": False}, {"id": "2", "title": "Hiện"}]}
    assert [j.title for j in ashby.parse_job_board(data, "x")] == ["Hiện"]


def test_workable_parse() -> None:
    jobs = workable.parse_widget(load("workable/huggingface_widget.json"), "huggingface")
    job = jobs[0]
    assert job.external_id == "huggingface:81B46579FE"
    assert job.company_name == "Hugging Face"
    assert job.locations == ["Paris, Île-de-France, France"]
    assert job.work_mode == "remote"
    assert job.url == "https://apply.workable.com/j/81B46579FE"
    assert job.posted_raw == "2026-05-29"


def test_workday_parse_detail_and_facets() -> None:
    facets = workday.vietnam_facets(load("workday/intel_jobs_page1.json")["facets"])
    assert facets == {"locations": ["1e4a4eb3adf101bf12a99b76bf812fcf"]}
    ref = ats.match_url("https://intel.wd1.myworkdayjobs.com/External")
    assert ref is not None
    job = workday.parse_detail(load("workday/intel_job_financial_analyst.json"), ref=ref)
    assert job is not None
    assert job.external_id == "intel/external:JR0286267"
    assert job.company_name == "763 Intel Products Vietnam Co., Ltd."
    assert job.locations == ["Vietnam, Ho_Chi_Minh_City"]
    assert job.employment_type == "Full time"
    assert job.posted_raw == "Posted 30+ Days Ago"
    assert job.posted_at == datetime(2026, 8, 6, 17, 0, tzinfo=UTC)  # startDate 2026-08-07 (VN)
    assert job.extra["country"] == "Vietnam"


def test_workday_posting_key_is_the_req_id() -> None:
    """Ô đầu `bulletFields` có thể là nhãn "Spotlight Job" (Intel: cả 20 tin) — mã tin là ô có
    dạng mã, không thì đuôi `externalPath`; trùng `jobReqId` của trang chi tiết."""
    ref = ats.match_url("https://intel.wd1.myworkdayjobs.com/External")
    assert ref is not None
    postings = workday.parse_listing(load("workday/intel_jobs_page1.json"))
    keys = [workday.posting_key(p, ref=ref) for p in postings]
    assert len(set(keys)) == len(postings) == 20
    assert keys[0] == "intel/external:JR0288027"
    no_bullets = {"externalPath": "/job/US-Oregon-Hillsboro/Senior-Engineer_JR0282069-1"}
    assert workday.posting_key(no_bullets, ref=ref) == "intel/external:JR0282069"
    # Trang chi tiết không ghi `jobReqId`: dùng đúng mã của tin trong danh sách.
    listing = {"title": "Kỹ sư", "externalPath": "/job/VN/Ky-su_JR0286267", "bulletFields": []}
    detail = {"jobPostingInfo": {"title": "Kỹ sư", "id": "4dbebf3ac9b2"}}
    job = workday.parse_detail(detail, ref=ref, listing=listing)
    assert job is not None
    assert job.external_id == workday.posting_key(listing, ref=ref) == "intel/external:JR0286267"


def test_bamboohr_parse() -> None:
    items = bamboohr.parse_list(load("bamboohr/seek_list.json"))
    assert [i["id"] for i in items] == ["15", "19", "20"]
    job = bamboohr.parse_detail(load("bamboohr/seek_detail_15.json"), sub="seek", job_id="15")
    assert job is not None
    assert job.external_id == "seek:15"
    assert job.url == "https://seek.bamboohr.com/careers/15"
    assert job.locations == ["Mayfaird, London, City of, United Kingdom"]
    assert (job.employment_type, job.department, job.level) == (
        "Full-Time",
        "IT",
        "Manager/Supervisor",
    )
    assert job.posted_raw == "2025-11-10"


def test_recruitee_parse() -> None:
    jobs = recruitee.parse_offers(load("recruitee/bunq_offers.json"), "bunq")
    job = jobs[0]
    assert job.external_id == "bunq:2783543"
    assert job.company_name == "bunq"
    assert job.url == "https://careers.bunq.com/o/head-of-product-6"
    assert job.apply_url == "https://careers.bunq.com/o/head-of-product-6/c/new"
    assert job.locations == [
        "Amsterdam, Noord-Holland, Netherlands",
        "Sofia, Sofia (stolitsa), Bulgaria",
    ]
    assert job.work_mode == "hybrid"
    assert job.requirements
    assert job.requirements.startswith("<")
    assert job.posted_at == datetime(2026, 10, 15, 9, 31, 36, tzinfo=UTC)


def test_teamtailor_parse_rss() -> None:
    jobs = teamtailor.parse_rss(
        (FIXTURES / "teamtailor/career_jobs.rss").read_bytes(), "career.teamtailor.com"
    )
    assert len(jobs) == 3
    job = jobs[0]
    assert job.title == "UK Account Executive - SMB"
    assert job.url == "https://career.teamtailor.com/jobs/8118064-uk-account-executive-smb"
    assert job.external_id == "career.teamtailor.com:4ff07fd5-3ac0-4333-8f4c-955380396321"
    assert job.locations == ["London, United Kingdom"]
    assert (job.work_mode, job.department) == ("hybrid", "Sales")
    assert job.posted_at == datetime(2026, 7, 24, 7, 5, 16, tzinfo=UTC)
    assert job.description
    assert job.description.startswith("<h4>")


# --- nguồn qua FakeFetcher -------------------------------------------------------------------


async def test_greenhouse_source_fetches_api_with_ats_rate() -> None:
    fetcher = FakeFetcher()
    fetcher.add_file(
        "https://boards-api.greenhouse.io/v1/boards/gitlab/jobs?content=true",
        FIXTURES / "greenhouse/gitlab_jobs.json",
    )
    ref = ats.match_url("https://job-boards.greenhouse.io/gitlab")
    assert ref is not None
    source = ats.source_for(ref)
    assert source is not None
    s = session(fetcher, ats_rate=3.0)
    jobs = await collect(source, s)
    assert len(jobs) == 3
    [(_, _, kwargs)] = fetcher.calls
    assert kwargs["rate"] == 3.0
    assert kwargs["source"] == "test"
    assert s.records[0].items == 3


@pytest.mark.parametrize(
    ("ref_url", "api", "fixture"),
    [
        (
            "https://jobs.lever.co/palantir",
            "https://api.lever.co/v0/postings/palantir?mode=json",
            "lever/palantir_postings.json",
        ),
        (
            "https://jobs.ashbyhq.com/openai",
            "https://api.ashbyhq.com/posting-api/job-board/openai?includeCompensation=true",
            "ashby/openai_job_board.json",
        ),
        (
            "https://apply.workable.com/huggingface/",
            "https://apply.workable.com/api/v1/widget/accounts/huggingface?details=true",
            "workable/huggingface_widget.json",
        ),
        (
            "https://bunq.recruitee.com/",
            "https://bunq.recruitee.com/api/offers/",
            "recruitee/bunq_offers.json",
        ),
        (
            "https://career.teamtailor.com/jobs",
            "https://career.teamtailor.com/jobs.rss",
            "teamtailor/career_jobs.rss",
        ),
    ],
)
async def test_single_request_sources(ref_url: str, api: str, fixture: str) -> None:
    fetcher = FakeFetcher()
    fetcher.add_file(api, FIXTURES / fixture)
    ref = ats.match_url(ref_url)
    assert ref is not None
    source = ats.source_for(ref)
    assert source is not None
    jobs = await collect(source, session(fetcher))
    assert jobs
    assert all(j.method is ExtractionMethod.ATS_API and j.title and j.url for j in jobs)
    assert [url for _, url, _ in fetcher.calls] == [api]


async def test_source_stops_when_api_fails() -> None:
    ref = ats.match_url("https://jobs.lever.co/gone")
    assert ref is not None
    source = ats.source_for(ref)
    assert source is not None
    with pytest.raises(SourceError) as exc:
        await collect(source, session(FakeFetcher()))  # 404
    assert exc.value.outcome is FetchOutcome.ERROR

    fetcher = FakeFetcher()
    fetcher.add(
        "https://api.lever.co/v0/postings/gone?mode=json", status=403, outcome=FetchOutcome.BLOCKED
    )
    s = session(fetcher)
    with pytest.raises(SourceError) as exc:
        await collect(source, s)
    assert exc.value.outcome is FetchOutcome.BLOCKED
    assert s.records[0].outcome is FetchOutcome.BLOCKED


async def test_workday_applies_vietnam_facet_and_fetches_details() -> None:
    api = "https://intel.wd1.myworkdayjobs.com/wday/cxs/intel/External"
    page1 = (FIXTURES / "workday/intel_jobs_page1.json").read_bytes()
    vietnam = (FIXTURES / "workday/intel_jobs_vietnam_facet.json").read_bytes()
    bodies: list[dict[str, Any]] = []

    def listing(kwargs: dict[str, Any]) -> FetchResult:
        bodies.append(kwargs["json"])
        body = vietnam if kwargs["json"]["appliedFacets"] else page1
        return fetch_result(f"{api}/jobs", body, content_type="application/json")

    fetcher = FakeFetcher()
    fetcher.add_route(f"{api}/jobs", listing)
    fetcher.add_file(
        f"{api}/job/Vietnam-Ho_Chi_Minh_City/Financial-Analyst_JR0286267",
        FIXTURES / "workday/intel_job_financial_analyst.json",
    )
    ref = ats.match_url("https://intel.wd1.myworkdayjobs.com/External")
    assert ref is not None
    source = workday.WorkdaySource(ref)
    # 724 tin > sức chứa 2 trang × 20 → lọc theo facet Việt Nam (8 tin). Tin ở Việt Nam được
    # lấy chi tiết trước; "2 Locations" xếp cuối và vượt giới hạn 7 trang chi tiết.
    s = session(fetcher, max_listing_pages=2, max_detail_pages=7)
    jobs = await collect(source, s)
    assert [b["appliedFacets"] for b in bodies] == [
        {},
        {"locations": ["1e4a4eb3adf101bf12a99b76bf812fcf"]},
    ]
    assert len(jobs) == 8
    detailed = [j for j in jobs if j.description]
    assert [j.title for j in detailed] == ["Financial Analyst"]
    assert detailed[0].company_name == "763 Intel Products Vietnam Co., Ltd."
    # Chi tiết lỗi hoặc vượt giới hạn trang: vẫn giữ tin từ danh sách.
    skipped = [j for j in jobs if not j.description]
    assert len(skipped) == 7
    assert jobs[-1].title.startswith("Foundry Program Manager")
    assert jobs[-1].extra == {"detail": "skipped"}
    assert all(
        j.url.startswith("https://intel.wd1.myworkdayjobs.com/External/job/") for j in skipped
    )
    detail_calls = [url for _, url, _ in fetcher.calls if "/job/" in url]
    assert len(detail_calls) == 7
    # Tin cuối không được lấy chi tiết: nguồn chưa xong.
    assert s.truncated == ["detail_pages"]


async def test_bamboohr_source_and_missing_company() -> None:
    fetcher = FakeFetcher()
    fetcher.add_file("https://seek.bamboohr.com/careers/list", FIXTURES / "bamboohr/seek_list.json")
    fetcher.add_file(
        "https://seek.bamboohr.com/careers/15/detail", FIXTURES / "bamboohr/seek_detail_15.json"
    )
    source = bamboohr.BambooHRSource(ref_of("https://seek.bamboohr.com/careers"))
    s = session(fetcher)
    jobs = await collect(source, s)
    assert [j.external_id for j in jobs] == ["seek:15"]  # chi tiết 19, 20 lỗi → bỏ
    assert s.truncated == []  # 404: tin đã gỡ, không phải lỗi tạm
    capped = session(fetcher, max_detail_pages=1)
    await collect(source, capped)
    assert capped.truncated == ["detail_pages"]
    # Chi tiết đã tải ở lượt trước: dùng lại, không gọi API chi tiết.
    reusing = session(fetcher)
    reusing.known = KnownJobs.of([(jobs[0], jobs[0].url)])
    calls = len(fetcher.calls)
    again = await collect(source, reusing)
    assert [j.external_id for j in again] == ["seek:15"]
    assert not any("/15/detail" in u for _, u, _ in fetcher.calls[calls:])

    gone = FakeFetcher()
    gone.add(
        "https://nope.bamboohr.com/careers/list",
        "<html></html>",
        final_url="https://www.bamboohr.com/",
    )
    with pytest.raises(SourceError):
        await collect(bamboohr.BambooHRSource(ref_of("https://nope.bamboohr.com/")), session(gone))
