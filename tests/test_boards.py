from __future__ import annotations

import gzip
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from jobcrawler.db.models import Company, SourceProfile
from jobcrawler.fakes import FakeChatClient, FakeFetcher
from jobcrawler.llm import LLMClient
from jobcrawler.pipeline.board_index import _Existing, plan_fetches
from jobcrawler.pipeline.profiles import decide
from jobcrawler.schemas import FetchOutcome
from jobcrawler.sources.base import SourceError, SourceSession
from jobcrawler.sources.boards.base import (
    BoardEntry,
    ListingState,
    parse_sitemap,
    sitemap_body,
)
from jobcrawler.text import normalize_company_name

from .boards_fake import HOME, FakeBoard, fake_site, job_page, sitemap_index, urlset


def session(fetcher: FakeFetcher) -> SourceSession:
    return SourceSession("board:fakeboard", fetcher, LLMClient(FakeChatClient(["{}"]), model=None))


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
