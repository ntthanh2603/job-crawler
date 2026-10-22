from __future__ import annotations

import gzip
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from jobcrawler.common.schemas import ExtractionMethod, FetchOutcome, RawJob
from jobcrawler.common.text import company_match_key, normalize_company_name
from jobcrawler.database.models import Company, SourceProfile
from jobcrawler.pipeline.board_index import _Existing, blocked_profiles, plan_fetches
from jobcrawler.pipeline.profiles import decide
from jobcrawler.services.fakes import FakeChatClient, FakeFetcher
from jobcrawler.services.llm import LLMClient
from jobcrawler.sources.base import SourceError, SourceLimits, SourceSession
from jobcrawler.sources.boards.base import BoardEntry, BoardJob, ListingState
from jobcrawler.sources.boards.profile_source import BoardProfileSource
from jobcrawler.sources.sitemaps import parse_sitemap, sitemap_body

from .boards_fake import HOME, FakeBoard, fake_site, job_page, sitemap_index, urlset

ACME_NAME = "Công ty Cổ phần ACME"


def session(fetcher: FakeFetcher, **limits: Any) -> SourceSession:
    return SourceSession(
        "board:fakeboard",
        fetcher,
        LLMClient(FakeChatClient(["{}"]), model=None),
        SourceLimits(**limits),
    )


def test_parse_sitemap_and_gzip() -> None:
    is_index, items = parse_sitemap(sitemap_index("a.xml", "b.xml"))
    assert is_index
    assert [u for u, _ in items] == [f"{HOME}a.xml", f"{HOME}b.xml"]
    body = urlset(("viec-lam/ke-toan-1.html", "2026-10-15T08:00:00+07:00"), ("x", None))
    is_index, items = parse_sitemap(body)
    assert not is_index
    assert items[0] == (
        f"{HOME}viec-lam/ke-toan-1.html",
        datetime(2026, 10, 15, 1, 0, tzinfo=UTC),
    )
    assert items[1][1] is None
    assert sitemap_body(gzip.compress(body.encode())) == body
    assert sitemap_body(b"\x1f\x8bhong") is None  # file nén hỏng ≠ sitemap rỗng
    # CDATA và &amp;
    cdata = "<urlset><url><loc><![CDATA[https://b.vn/j?id=1&amp;x=2]]></loc></url></urlset>"
    assert parse_sitemap(cdata)[1] == [("https://b.vn/j?id=1&x=2", None)]


async def test_sitemap_board_entries_follow_job_sitemaps_only() -> None:
    fetcher = fake_site(
        {
            "viec-lam/ke-toan-11.html": ("Kế toán", "ACME", "cong-ty/acme-7"),
            "viec-lam/tin-tuc-khong-phai-tin": ("x", "x", "x"),
        },
        lastmod={"viec-lam/ke-toan-11.html": "2026-10-15"},
    )
    state = ListingState()
    entries = [e async for e in FakeBoard().entries(session(fetcher), state)]
    assert entries == [
        BoardEntry(
            url=f"{HOME}viec-lam/ke-toan-11.html",
            external_id="11",
            lastmod=datetime(2026, 10, 14, 17, 0, tzinfo=UTC),
        )
    ]
    assert state.complete
    assert state.sitemaps == 2
    assert f"{HOME}sitemap-news.xml" not in [u for _, u, _ in fetcher.calls]


async def test_sitemap_board_listing_errors() -> None:
    board = FakeBoard()
    with pytest.raises(SourceError):
        _ = [e async for e in board.entries(session(FakeFetcher()), ListingState())]
    fetcher = FakeFetcher()
    fetcher.add(f"{HOME}sitemap.xml", sitemap_index("sitemap-jobs-1.xml"))
    state = ListingState()
    assert [e async for e in board.entries(session(fetcher), state)] == []
    assert not state.complete  # sitemap con lỗi → danh sách chưa đủ
    # Sitemap con nén hỏng: cũng là lỗi, không phải sitemap rỗng.
    broken = FakeFetcher()
    broken.add(f"{HOME}sitemap.xml", sitemap_index("sitemap-jobs-1.xml"))
    broken.add(f"{HOME}sitemap-jobs-1.xml", b"\x1f\x8bhong")
    state = ListingState()
    assert [e async for e in board.entries(session(broken), state)] == []
    assert not state.complete
    blocked = FakeFetcher()
    blocked.add(f"{HOME}sitemap.xml", status=403, outcome=FetchOutcome.BLOCKED)
    with pytest.raises(SourceError) as exc:
        _ = [e async for e in board.entries(session(blocked), ListingState())]
    assert exc.value.outcome is FetchOutcome.BLOCKED


class MemoryIndex:
    """`ProfileIndex` trong bộ nhớ."""

    def __init__(self, jobs: list[RawJob], others: list[RawJob] | None = None) -> None:
        self.jobs = jobs
        self.others = {j.external_id: j for j in others or []}
        self.asked: list[list[str]] = []

    async def for_profile(self) -> list[RawJob]:
        return list(self.jobs)

    async def by_ids(self, external_ids: Sequence[str]) -> dict[str, RawJob]:
        self.asked.append(list(external_ids))
        return {i: self.others[i] for i in external_ids if i in self.others}


def indexed_job(n: int, title: str = "Tin cũ") -> RawJob:
    return RawJob(
        source=FakeBoard.name,
        external_id=str(n),
        url=f"{HOME}viec-lam/tin-{n}.html",
        title=title,
        method=ExtractionMethod.JSONLD,
    )


class PagedBoard(FakeBoard):
    profile_listing_complete = True


PROFILE = f"{HOME}cong-ty/acme-7"


def profile_site(*pages: list[int]) -> FakeFetcher:
    """Trang profile chia trang: trang i liệt kê các tin trong `pages[i]`."""
    fetcher = fake_site(
        {
            f"viec-lam/tin-{n}.html": (f"Tin {n}", ACME_NAME, "cong-ty/acme-7")
            for p in pages
            for n in p
        }
    )
    for i, numbers in enumerate(pages, start=1):
        links = "".join(f'<a href="/viec-lam/tin-{n}.html">Tin {n}</a>' for n in numbers)
        if i < len(pages):
            links += f'<a href="/cong-ty/acme-7?page={i + 1}">{i + 1}</a>'
        fetcher.add(PROFILE if i == 1 else f"{PROFILE}?page={i}", f"<h1>ACME</h1>{links}")
    return fetcher


async def collect_profile(source: BoardProfileSource, s: SourceSession) -> list[RawJob]:
    return [job async for job in source.jobs(s)]


async def test_profile_source_merges_listing_with_index() -> None:
    # Index có tin 1 (còn trên trang) và 9 (đã đóng); trang profile 2 trang: 1, 2 | 3.
    index = MemoryIndex(
        [indexed_job(1), indexed_job(9)], others=[indexed_job(2, "Chưa gắn profile")]
    )
    fetcher = profile_site([1, 2], [3])
    source = BoardProfileSource(
        PagedBoard(), external_id="7", url=PROFILE, fresh=False, index=index, name=ACME_NAME
    )
    s = session(fetcher)
    jobs = await collect_profile(source, s)
    # Tin 9 không còn trên danh sách đủ → bỏ; tin 2 có trong index (chưa gắn profile) → không
    # tải lại; chỉ tải chi tiết tin 3.
    assert [(j.external_id, j.title) for j in jobs] == [
        ("1", "Tin cũ"),
        ("2", "Chưa gắn profile"),
        ("3", "Tin 3"),
    ]
    assert index.asked == [["2", "3"]]
    assert [u for _, u, _ in fetcher.calls] == [
        PROFILE,
        f"{PROFILE}?page=2",
        f"{HOME}viec-lam/tin-3.html",
    ]
    assert s.truncated == []

    # Không chắc danh sách đủ (adapter chưa kiểm phân trang): giữ mọi tin index.
    loose = BoardProfileSource(
        FakeBoard(), external_id="7", url=PROFILE, fresh=False, index=index, name=ACME_NAME
    )
    jobs = await collect_profile(loose, session(profile_site([1, 2], [3])))
    assert {j.external_id for j in jobs} == {"1", "9", "2", "3"}

    # Index còn mới: không gọi mạng.
    quiet = FakeFetcher()
    fresh = BoardProfileSource(
        PagedBoard(), external_id="7", url=PROFILE, fresh=True, index=index, name=ACME_NAME
    )
    assert [j.external_id for j in await collect_profile(fresh, session(quiet))] == ["1", "9"]
    assert quiet.calls == []


async def test_profile_source_without_network_serves_index_only() -> None:
    """Lượt poll ban ngày: index cũ cũng không gọi trang tuyển dụng (lượt đuôi giữ tin mới)."""
    index = MemoryIndex([indexed_job(1), indexed_job(9)])
    quiet = FakeFetcher()
    offline = BoardProfileSource(
        PagedBoard(),
        external_id="7",
        url=PROFILE,
        fresh=False,
        index=index,
        name=ACME_NAME,
        network=False,
    )
    assert [j.external_id for j in await collect_profile(offline, session(quiet))] == ["1", "9"]
    assert quiet.calls == []
    assert index.asked == []


async def test_profile_source_falls_back_to_index_and_skips_foreign_jobs() -> None:
    index = MemoryIndex([indexed_job(1)])
    blocked = FakeFetcher()
    blocked.add(PROFILE, status=403, outcome=FetchOutcome.BLOCKED)
    source = BoardProfileSource(
        PagedBoard(), external_id="7", url=PROFILE, fresh=False, index=index, name=ACME_NAME
    )
    got: list[RawJob] = []

    async def drain() -> None:
        async for job in source.jobs(session(blocked)):
            got.append(job)

    with pytest.raises(SourceError):
        await drain()
    assert [j.external_id for j in got] == ["1"]  # trang profile bị chặn: dùng tin đã index

    # Khối "việc làm tương tự" trên trang profile: tin của nhà tuyển dụng khác thì bỏ.
    fetcher = fake_site(
        {
            "viec-lam/tin-5.html": ("Tin 5", ACME_NAME, "cong-ty/acme-7"),
            "viec-lam/tin-6.html": ("Tin 6", "Công ty TNHH Khác Hẳn", "cong-ty/khac-8"),
        }
    )
    fetcher.add(PROFILE, '<a href="/viec-lam/tin-5.html">5</a><a href="/viec-lam/tin-6.html">6</a>')
    jobs = await collect_profile(
        BoardProfileSource(
            PagedBoard(),
            external_id="7",
            url=PROFILE,
            fresh=False,
            index=MemoryIndex([]),
            name=ACME_NAME,
        ),
        session(fetcher),
    )
    assert [j.external_id for j in jobs] == ["5"]

    # Chạm giới hạn trang chi tiết: nguồn chưa xong.
    many = profile_site(list(range(10, 16)))
    s = session(many, max_detail_pages=2)
    jobs = await collect_profile(
        BoardProfileSource(
            PagedBoard(),
            external_id="7",
            url=PROFILE,
            fresh=False,
            index=MemoryIndex([]),
            name=ACME_NAME,
        ),
        s,
    )
    assert len(jobs) == 2
    assert s.truncated == ["detail_pages"]


class FlakyBoard(PagedBoard):
    def parse_job(self, html: str, url: str) -> BoardJob | None:
        if url.endswith("tin-2.html"):
            raise ValueError("bố cục lạ")
        return super().parse_job(html, url)


async def test_profile_source_survives_a_page_that_fails_to_parse() -> None:
    """Một trang chi tiết bóc lỗi chỉ mất tin đó (lưu vào trang lỗi), không hỏng cả nguồn."""
    s = session(profile_site([1, 2, 3]))
    source = BoardProfileSource(
        FlakyBoard(),
        external_id="7",
        url=PROFILE,
        fresh=False,
        index=MemoryIndex([]),
        name=ACME_NAME,
    )
    assert [j.external_id for j in await collect_profile(source, s)] == ["1", "3"]
    [failed] = [r for r in s.records if r.failure]
    assert failed.url == f"{HOME}viec-lam/tin-2.html"
    assert (failed.failure or "").startswith("parse_error: ValueError")


def test_bad_dates_in_sitemap_and_job_page_are_ignored() -> None:
    """Ngày không có thật ("0000-00-00", "2026-02-30") là không có ngày, không làm hỏng trang."""
    _, items = parse_sitemap(urlset(("viec-lam/tin-1.html", "0000-00-00")))
    assert items == [(f"{HOME}viec-lam/tin-1.html", None)]
    html = job_page("Kế toán", ACME_NAME, "cong-ty/acme-7", valid_through="2026-02-30")
    parsed = FakeBoard().parse_job(html, f"{HOME}viec-lam/ke-toan-1.html")
    assert parsed is not None
    assert (parsed.job.expires_at, parsed.job.expires_raw) == (None, "2026-02-30")


def test_parse_job_with_profile() -> None:
    url = f"{HOME}viec-lam/ke-toan-11.html"
    parsed = FakeBoard().parse_job(job_page("Kế toán", "ACME JSC", "cong-ty/acme-7"), url)
    assert parsed is not None
    assert parsed.job.source == "fakeboard"
    assert parsed.job.external_id == "11"
    assert parsed.job.url == url
    assert parsed.job.title == "Kế toán"
    assert parsed.profile is not None
    assert parsed.profile.external_id == "7"
    assert parsed.profile.url == f"{HOME}cong-ty/acme-7"
    assert parsed.profile.name == "ACME JSC"
    assert parsed.profile.website is None  # sameAs trỏ về chính trang tuyển dụng
    assert parsed.profile.info == {"logo": f"{HOME}logo.png"}
    assert FakeBoard().parse_job("<html>không có JSON-LD</html>", url) is None
    assert FakeBoard().parse_job(job_page("a", "b", "cong-ty/c-1"), f"{HOME}khac") is None


def test_blocked_profiles() -> None:
    def row(profile: int | None, status: str = "active") -> _Existing:
        return _Existing(
            id=UUID(int=99),
            lastmod=None,
            fetched_at=None,
            status=status,
            etag=None,
            http_last_modified=None,
            content_hash=None,
            source_profile_id=UUID(int=profile) if profile is not None else None,
        )

    assert blocked_profiles([]) == set()
    # Tin đã có trong index (nội dung cũ): profile vẫn đủ tin.
    assert blocked_profiles([row(1), row(2)]) == set()
    assert blocked_profiles([row(1, "removed"), row(2)]) == {UUID(int=1)}
    # Tin mới / chưa gắn profile: không biết của profile nào.
    assert blocked_profiles([row(1, "removed"), None]) is None
    assert blocked_profiles([row(None)]) is None


def test_plan_fetches_order() -> None:
    now = datetime(2026, 10, 17, tzinfo=UTC)
    old = now - timedelta(days=30)
    recent = now - timedelta(days=1)

    def row(
        id_: int,
        *,
        lastmod: datetime | None = None,
        fetched: datetime | None = recent,
        status: str = "active",
    ) -> _Existing:
        return _Existing(UUID(int=id_), lastmod, fetched, status, None, None, "h")

    entries = {
        k: BoardEntry(url=f"u{k}", external_id=k, lastmod=lm)
        for k, lm in [
            ("new", None),
            ("same", recent),
            ("changed", now),
            ("stale", None),
            ("gone", None),
        ]
    }
    existing = {
        "same": row(1, lastmod=recent),
        "changed": row(2, lastmod=recent),
        "stale": row(3, fetched=old),
        "gone": row(4, status="removed"),
    }
    plan = plan_fetches(entries, existing, now=now, refresh_days=7, full=False)
    assert [e.external_id for e, _ in plan] == ["new", "changed", "gone", "stale"]
    full = plan_fetches(entries, existing, now=now, refresh_days=7, full=True)
    assert len(full) == 5


def _profile(name: str, *, website: str | None = None, tax: str | None = None) -> SourceProfile:
    return SourceProfile(
        id=UUID(int=1),
        source="fakeboard",
        external_id="7",
        url=f"{HOME}cong-ty/x-7",
        name_on_source=name,
        name_norm=normalize_company_name(name),
        website=website,
        tax_code=tax,
        info={},
    )


def _company(
    id_: int,
    name: str,
    *,
    domain: str | None = None,
    tax: str | None = None,
    kyc: str | None = "verified",
) -> Company:
    return Company(
        id=id_,
        input_name=name,
        legal_name=name,
        primary_domain=domain,
        other_domains=[],
        tax_code=tax,
        kyc_status=kyc,
    )


def test_profile_policy_by_kyc_status() -> None:
    from jobcrawler.common.schemas import KycStatus
    from jobcrawler.modules.boards import boards_service
    from jobcrawler.modules.crawl.company_service import profile_policy

    assert profile_policy(KycStatus.VERIFIED, name_profiles=True) == "all"
    assert profile_policy(KycStatus.NOT_FOUND, name_profiles=True) == "exact_name"
    assert profile_policy(KycStatus.NOT_FOUND, name_profiles=False) == "strong"
    assert profile_policy(KycStatus.AMBIGUOUS, name_profiles=True) == "strong"
    assert profile_policy(None, name_profiles=True) == "strong"

    def mapped(name: str, signal: str) -> SourceProfile:
        profile = _profile(name)
        profile.match_evidence = {"signal": signal, "rule": signal != "llm"}
        return profile

    usable = boards_service._usable  # pyright: ignore[reportPrivateUsage]
    names = ["KiotViet", "Công ty TNHH Thương mại Dịch vụ Kiotviet"]
    assert usable(mapped("KIOTVIET", "name"), "exact_name", names)
    assert not usable(mapped("KIOTVIET", "name"), "strong", names)
    assert usable(mapped("Tên khác hẳn", "website"), "strong", names)
    # LLM chọn, hay tên chung chung: không đủ khi KYC không tìm thấy website.
    assert not usable(mapped("KIOTVIET", "llm"), "exact_name", names)
    # Khoá tên phải trùng hẳn một tên của doanh nghiệp.
    assert not usable(mapped("KIOTVIET", "name"), "exact_name", names[1:])
    generic = ["Công ty TNHH Thương mại Dịch vụ"]
    assert not usable(mapped("Thương mại Dịch vụ", "name"), "exact_name", generic)


def test_decide_maps_profile_name_variants() -> None:
    mb = _company(5, "Ngân hàng TMCP Quân Đội")
    profile = _profile("Ngân Hàng Thương Mại Cổ Phần Quân Đội (MB Bank)")
    profile.name_norm = company_match_key(profile.name_on_source)
    d = decide(profile, [(mb, company_match_key(mb.input_name), 1.0)])
    assert d is not None
    assert (d.company_id, d.evidence["signal"]) == (5, "name")
    # Đầu vào là một chi nhánh: profile của chi nhánh khác không khớp; profile chung thì khớp.
    hcm = _company(7, "Ngân hàng KEB Hana - Chi nhánh Thành phố Hồ Chí Minh")
    for name, expected in [
        ("Ngân Hàng Keb Hana - Chi Nhánh Hà Nội", None),
        ("Ngân Hàng Keb Hana - Chi nhánh Thành phố Hồ Chí Minh", 7),
    ]:
        branch = _profile(name)
        branch.name_norm = company_match_key(name)
        d = decide(branch, [(hcm, company_match_key(hcm.input_name), 1.0)])
        assert d is not None
        assert d.company_id == expected, name
    # "FPT Software" không phải "FPT".
    fpt = _company(6, "FPT")
    other = _profile("FPT Software")
    other.name_norm = company_match_key(other.name_on_source)
    d = decide(other, [(fpt, company_match_key(fpt.input_name), 0.6)])
    assert d is not None
    assert d.company_id is None


def test_decide_does_not_map_by_name_across_legal_forms() -> None:
    """Cùng khoá tên nhưng khác loại hình pháp lý (danh bạ nhà tuyển dụng có hàng trăm nghìn tên):
    không map bằng tên, chờ website / MST hay LLM."""
    hpg = _company(8, "Công ty Cổ phần Tập đoàn Hòa Phát")
    for name in ("Công ty TNHH Hòa Phát", "cong ty tnhh hoa phat"):
        profile = _profile(name)
        profile.name_norm = company_match_key(name)
        assert decide(profile, [(hpg, company_match_key(hpg.input_name), 1.0)]) is None, name
    same = _profile("cong ty co phan tap doan hoa phat")
    same.name_norm = company_match_key(same.name_on_source)
    d = decide(same, [(hpg, company_match_key(hpg.input_name), 1.0)])
    assert d is not None
    assert d.company_id == 8


def test_decide_profile_match_rules() -> None:
    acme = _company(1, "Công ty Cổ phần ACME Việt Nam", domain="acme.vn", tax="0101234567")
    other = _company(2, "Công ty TNHH ACME Logistics", domain="acmelog.vn")
    norm = normalize_company_name

    # MST trùng: chắc chắn.
    d = decide(_profile("ACME", tax="0101234567"), [(acme, norm(acme.input_name), 0.7)])
    assert d is not None
    assert (d.company_id, d.confidence, d.evidence["signal"]) == (1, 1.0, "mst")
    # Website trùng domain.
    d = decide(
        _profile("ACME VN", website="https://www.acme.vn/"),
        [(acme, norm(acme.input_name), 0.7), (other, norm(other.input_name), 0.65)],
    )
    assert d is not None
    assert (d.company_id, d.evidence["signal"]) == (1, "website")
    # Tên khớp đúng một doanh nghiệp.
    d = decide(_profile("CÔNG TY CP ACME VIỆT NAM"), [(acme, norm(acme.input_name), 1.0)])
    assert d is not None
    assert (d.company_id, d.confidence) == (1, 0.85)
    # Hai doanh nghiệp cùng tên sau chuẩn hoá: rule không quyết, để LLM chọn.
    twin = _company(4, "ACME Việt Nam (Hải Phòng)", kyc="ambiguous")
    assert (
        decide(
            _profile("ACME VIỆT NAM"), [(acme, "acme viet nam", 1.0), (twin, "acme viet nam", 1.0)]
        )
        is None
    )
    # Tên chỉ gần giống (viết liền lệch nhiều): không coi là ứng viên.
    d = decide(_profile("ACME"), [(acme, "acme viet nam", 0.6), (other, "acme logistics", 0.6)])
    assert d is not None
    assert d.company_id is None
    # Khác MST → loại; khác số → loại.
    d = decide(_profile("ACME Việt Nam", tax="0309999999"), [(acme, norm(acme.input_name), 0.9)])
    assert d is not None
    assert d.company_id is None
    so1 = _company(3, "Công ty Xây dựng số 1")
    d = decide(_profile("Công ty Xây dựng số 2"), [(so1, norm(so1.input_name), 0.83)])
    assert d is not None
    assert d.company_id is None
    # Website khác domain đã xác minh → loại.
    d = decide(
        _profile("ACME Việt Nam", website="https://acme-fake.com"),
        [(acme, norm(acme.input_name), 0.9)],
    )
    assert d is not None
    assert d.company_id is None
