"""Lượt đuôi (`BoardAdapter.recent`): chỉ phần mới nhất của danh sách, vài request, không bao giờ
là danh sách đủ. Mẫu thật ở tests/fixtures/boards/<trang>/."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from jobcrawler.common.schemas import FetchOutcome
from jobcrawler.services.fakes import FakeChatClient, FakeFetcher, fetch_result
from jobcrawler.services.llm import LLMClient
from jobcrawler.services.net.http import FetchResult
from jobcrawler.sources import boards
from jobcrawler.sources.base import SourceError, SourceSession
from jobcrawler.sources.boards.base import (
    BoardAdapter,
    BoardEntry,
    KnownIds,
    ListingBoard,
    ListingState,
    if_modified_since,
    newest_first,
    reached_known,
)
from jobcrawler.sources.boards.vietnamworks import API as VNW_API

from .boards_fake import HOME, FakeBoard, urlset

FIXTURES = Path(__file__).parent / "fixtures" / "boards"
REGISTRY = boards.registry()
XML = "application/xml"


def session(fetcher: FakeFetcher) -> SourceSession:
    return SourceSession("board:test", fetcher, LLMClient(FakeChatClient(["{}"]), model=None))


async def recent(
    adapter: BoardAdapter,
    fetcher: FakeFetcher,
    *,
    limit: int = 1000,
    since: datetime | None = None,
    known: KnownIds | None = None,
) -> tuple[list[BoardEntry], ListingState]:
    state = ListingState()
    found = [
        e
        async for e in adapter.recent(
            session(fetcher), state, limit=limit, since=since, known=known
        )
    ]
    assert not state.complete  # phần đuôi không bao giờ là danh sách đủ
    assert len({e.external_id for e in found}) == len(found)
    for entry in found:
        assert entry.url == adapter.canonical(entry.url)
        assert adapter.job_id(entry.url) == entry.external_id
    return found, state


def urls(fetcher: FakeFetcher) -> list[str]:
    return [u for _, u, _ in fetcher.calls]


def fixture(board: str, name: str) -> bytes:
    return (FIXTURES / board / name).read_bytes()


def known_ids(ids: set[str]) -> KnownIds:
    async def known(external_ids: Sequence[str]) -> set[str]:
        return {i for i in external_ids if i in ids}

    return known


def all_known() -> KnownIds:
    async def known(external_ids: Sequence[str]) -> set[str]:
        return set(external_ids)

    return known


def index_xml(*children: tuple[str, str | None]) -> str:
    items = "".join(
        f"<sitemap><loc>{HOME}{c}</loc>" + (f"<lastmod>{lm}</lastmod>" if lm else "") + "</sitemap>"
        for c, lm in children
    )
    return (
        f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{items}</sitemapindex>'
    )


def job(n: int) -> str:
    return f"viec-lam/tin-{n}.html"


# --- khung chung ---------------------------------------------------------------------------------


def test_newest_first_orders_by_lastmod_then_later_position() -> None:
    def entry(i: str, lastmod: datetime | None) -> BoardEntry:
        return BoardEntry(url=f"u{i}", external_id=i, lastmod=lastmod)

    day = datetime(2026, 10, 19, tzinfo=UTC)
    noon = datetime(2026, 10, 19, 12, tzinfo=UTC)
    ordered = newest_first(
        [entry("a", day), entry("b", noon), entry("c", None), entry("d", day), entry("e", None)]
    )
    # Cùng `lastmod` (hay không có): tin đứng sau trong sitemap được coi là mới hơn.
    assert [e.external_id for e in ordered] == ["b", "d", "a", "e", "c"]


def test_if_modified_since_backs_off_for_clock_skew() -> None:
    assert if_modified_since(None) is None
    header = if_modified_since(datetime(2026, 10, 19, 5, 0, tzinfo=UTC))
    assert header == {"If-Modified-Since": "Mon, 19 Oct 2026 04:55:00 GMT"}


async def test_reached_known_needs_half_the_page() -> None:
    assert not await reached_known(None, ["1", "2"])
    assert not await reached_known(all_known(), [])
    assert await reached_known(known_ids({"1"}), ["1", "2"])
    assert not await reached_known(known_ids({"1"}), ["1", "2", "3"])


async def test_sitemap_tail_reads_only_newest_child_sitemaps() -> None:
    fetcher = FakeFetcher()
    # Index không ghi `lastmod`: file số lớn nhất (sắp theo số, không theo chữ).
    children = [f"sitemap-jobs-{n}.xml" for n in (1, 2, 10, 9, 3)]
    fetcher.add(f"{HOME}sitemap.xml", index_xml(*((c, None) for c in children)))
    fetcher.add(
        f"{HOME}sitemap-jobs-9.xml",
        urlset((job(1), "2026-10-19T08:00:00+07:00"), (job(2), "2026-10-19T09:00:00+07:00")),
        content_type=XML,
    )
    fetcher.add(
        f"{HOME}sitemap-jobs-10.xml",
        urlset((job(3), "2026-10-19T08:30:00+07:00"), (job(4), "2026-10-18T08:00:00+07:00")),
        content_type=XML,
    )
    found, state = await recent(FakeBoard(), fetcher)
    assert urls(fetcher) == [
        f"{HOME}sitemap.xml",
        f"{HOME}sitemap-jobs-9.xml",
        f"{HOME}sitemap-jobs-10.xml",
    ]
    assert [e.external_id for e in found] == ["2", "3", "1", "4"]  # `lastmod` mới trước
    assert state.sitemaps == 3
    assert state.errors == []

    # `limit`: chỉ chừng ấy tin mới nhất.
    limited, _ = await recent(FakeBoard(), fetcher, limit=2)
    assert [e.external_id for e in limited] == ["2", "3"]


async def test_sitemap_tail_picks_children_by_index_lastmod() -> None:
    fetcher = FakeFetcher()
    fetcher.add(
        f"{HOME}sitemap.xml",
        index_xml(
            ("sitemap-jobs-1.xml", "2026-10-19T09:00:00+07:00"),
            ("sitemap-jobs-2.xml", "2026-10-01T09:00:00+07:00"),
            ("sitemap-jobs-3.xml", "2026-10-18T09:00:00+07:00"),
        ),
    )
    fetcher.add(f"{HOME}sitemap-jobs-1.xml", urlset((job(1), None)), content_type=XML)
    fetcher.add(f"{HOME}sitemap-jobs-3.xml", urlset((job(3), None)), content_type=XML)
    found, _ = await recent(FakeBoard(), fetcher)
    assert f"{HOME}sitemap-jobs-2.xml" not in urls(fetcher)
    # Không có `lastmod` từng tin: file mới hơn (đọc sau) trước.
    assert [e.external_id for e in found] == ["1", "3"]


async def test_sitemap_tail_errors() -> None:
    # Không đọc được gì: lỗi nguồn (như sitemap gốc lỗi ở lượt đủ).
    with pytest.raises(SourceError):
        await recent(FakeBoard(), FakeFetcher())
    blocked = FakeFetcher()
    blocked.add(f"{HOME}sitemap.xml", status=403, outcome=FetchOutcome.BLOCKED)
    with pytest.raises(SourceError) as exc:
        await recent(FakeBoard(), blocked)
    assert exc.value.outcome is FetchOutcome.BLOCKED
    # Một sitemap con lỗi: phần còn lại vẫn dùng được, lượt ghi lỗi.
    fetcher = FakeFetcher()
    fetcher.add(
        f"{HOME}sitemap.xml", index_xml(("sitemap-jobs-1.xml", None), ("sitemap-jobs-2.xml", None))
    )
    fetcher.add(f"{HOME}sitemap-jobs-2.xml", urlset((job(5), None)), content_type=XML)
    found, state = await recent(FakeBoard(), fetcher)
    assert [e.external_id for e in found] == ["5"]
    assert state.errors == [f"sitemap lỗi: {HOME}sitemap-jobs-1.xml"]


class ConditionalBoard(FakeBoard):
    tail_sitemaps = (f"{HOME}sitemap-jobs-1.xml",)
    tail_conditional = True


async def test_conditional_tail_skips_unchanged_sitemap() -> None:
    sitemap = f"{HOME}sitemap-jobs-1.xml"
    since = datetime(2026, 10, 19, 5, 0, tzinfo=UTC)
    seen: list[dict[str, str]] = []

    def conditional(kwargs: dict[str, Any]) -> FetchResult:
        headers = kwargs.get("headers") or {}
        seen.append(dict(headers))
        if headers.get("If-Modified-Since"):
            return fetch_result(sitemap, status=304)
        return fetch_result(sitemap, urlset((job(7), None)), content_type=XML)

    fetcher = FakeFetcher()
    fetcher.add_route(sitemap, conditional)
    # Chưa có lượt trước: tải cả file, không hỏi If-Modified-Since.
    found, _ = await recent(ConditionalBoard(), fetcher)
    assert [e.external_id for e in found] == ["7"]
    # Có lượt trước: 304 = không có tin mới, không phải lỗi.
    found, state = await recent(ConditionalBoard(), fetcher, since=since)
    assert found == []
    assert state.errors == []
    assert seen == [{}, {"If-Modified-Since": "Mon, 19 Oct 2026 04:55:00 GMT"}]
    assert urls(fetcher) == [sitemap, sitemap]  # thẳng sitemap tin, không qua sitemap gốc
    # Sitemap không bật `tail_conditional`: không gửi header.
    plain = FakeFetcher()
    plain.add(f"{HOME}sitemap.xml", index_xml(("sitemap-jobs-1.xml", None)))
    plain.add(sitemap, urlset((job(7), None)), content_type=XML)
    await recent(FakeBoard(), plain, since=since)
    assert all(not kwargs.get("headers") for _, _, kwargs in plain.calls)


class PagedListing(ListingBoard):
    name = "pagedlisting"
    home = HOME
    listing_url = f"{HOME}viec-lam?page={{page}}"
    job_url = FakeBoard.job_url
    profile_url = FakeBoard.profile_url
    tail_max_pages = 3


def listing_site(*pages: list[int]) -> FakeFetcher:
    fetcher = FakeFetcher()
    for i, numbers in enumerate(pages, start=1):
        links = "".join(f'<a href="/{job(n)}">Tin {n}</a>' for n in numbers)
        fetcher.add(f"{HOME}viec-lam?page={i}", f"<html><body>{links}</body></html>")
    return fetcher


async def test_listing_tail_stops_at_indexed_part() -> None:
    pages = ([9, 8, 7, 6], [5, 4, 3, 2], [1, 0, 11, 12], [13, 14, 15, 16])
    # Trang 1 phần lớn đã có: dừng ngay, 1 request.
    fetcher = listing_site(*pages)
    found, state = await recent(PagedListing(), fetcher, known=known_ids({"8", "7", "6"}))
    assert [e.external_id for e in found] == ["9", "8", "7", "6"]
    assert urls(fetcher) == [f"{HOME}viec-lam?page=1"]
    assert state.sitemaps == 1
    # Còn tồn nhiều tin mới: đọc tiếp tới khi chạm phần đã có.
    fetcher = listing_site(*pages)
    found, _ = await recent(PagedListing(), fetcher, known=known_ids({"5", "4", "3"}))
    assert len(found) == 8
    assert len(fetcher.calls) == 2
    # Không bao giờ quá `tail_max_pages` trang, kể cả khi toàn tin mới.
    fetcher = listing_site(*pages)
    found, _ = await recent(PagedListing(), fetcher, known=known_ids(set()))
    assert len(found) == 12
    assert len(fetcher.calls) == 3
    # `limit` chặn trước: không tải trang sau.
    fetcher = listing_site(*pages)
    found, _ = await recent(PagedListing(), fetcher, limit=3, known=known_ids(set()))
    assert [e.external_id for e in found] == ["9", "8", "7"]
    assert len(fetcher.calls) == 1


async def test_listing_tail_errors() -> None:
    with pytest.raises(SourceError):
        await recent(PagedListing(), FakeFetcher())
    fetcher = listing_site([3, 2])  # trang 2 lỗi (404)
    found, state = await recent(PagedListing(), fetcher, known=known_ids(set()))
    assert [e.external_id for e in found] == ["3", "2"]
    assert state.errors == [f"trang danh sách lỗi: {HOME}viec-lam?page=2"]


# --- từng trang (mẫu thật) ------------------------------------------------------------------------


async def test_topcv_tail_reads_last_job_sitemaps() -> None:
    topcv = REGISTRY["topcv"]
    fetcher = FakeFetcher()
    # sitemap/jobs.xml → jobs_0..7.xml, không có `lastmod`.
    fetcher.add("https://www.topcv.vn/sitemap/jobs.xml", fixture("topcv", "sitemap_jobs_index.xml"))
    fetcher.add(
        "https://www.topcv.vn/sitemap/jobs_5.xml",
        fixture("topcv", "sitemap_jobs.xml"),
        content_type=XML,
    )
    fetcher.add(
        "https://www.topcv.vn/sitemap/jobs_6.xml",
        fixture("topcv", "sitemap_jobs_newest.xml"),
        content_type=XML,
    )
    found, state = await recent(topcv, fetcher)
    assert urls(fetcher) == [
        "https://www.topcv.vn/sitemap/jobs.xml",
        "https://www.topcv.vn/sitemap/jobs_5.xml",
        "https://www.topcv.vn/sitemap/jobs_6.xml",
        "https://www.topcv.vn/sitemap/jobs_7.xml",
    ]
    assert len(found) == 23
    lastmods = [e.lastmod for e in found if e.lastmod is not None]
    assert len(lastmods) == len(found)
    assert lastmods == sorted(lastmods, reverse=True)
    assert found[0].external_id == "2134520"  # sửa gần nhất (04:15:58 UTC)
    assert found[1].external_id == "2334005"
    assert state.errors == ["sitemap lỗi: https://www.topcv.vn/sitemap/jobs_7.xml"]
    limited, _ = await recent(topcv, fetcher, limit=5)
    assert [e.external_id for e in limited] == [e.external_id for e in found[:5]]


async def test_jobsgo_tail_asks_if_modified_since() -> None:
    jobsgo = REGISTRY["jobsgo"]
    sitemap = "https://jobsgo.vn/sitemap-job.xml"
    body = fixture("jobsgo", "sitemap_jobs.xml")

    def reply(kwargs: dict[str, Any]) -> FetchResult:
        if (kwargs.get("headers") or {}).get("If-Modified-Since"):
            return fetch_result(sitemap, status=304)
        return fetch_result(sitemap, body, content_type=XML)

    fetcher = FakeFetcher()
    fetcher.add_route(sitemap, reply)
    found, _ = await recent(jobsgo, fetcher, limit=3)
    assert [e.external_id for e in found] == ["29378492396", "29378478817", "29378451659"]
    assert urls(fetcher) == [sitemap]  # thẳng sitemap tin, 1 request
    found, state = await recent(jobsgo, fetcher, since=datetime(2026, 10, 19, tzinfo=UTC))
    assert found == []
    assert state.errors == []


async def test_careerviet_tail_reads_todays_sitemap() -> None:
    careerviet = REGISTRY["careerviet"]
    fetcher = FakeFetcher()
    fetcher.add(
        "https://careerviet.vn/sitemap/job_current_date.xml",
        fixture("careerviet", "sitemap_jobs_current_date.xml"),
        content_type=XML,
    )
    found, _ = await recent(careerviet, fetcher)
    assert len(fetcher.calls) == 1
    # Chỉ bản vi; `lastmod` chỉ có ngày nên tin sau trong file (mã lớn hơn) trước.
    assert [e.external_id for e in found] == [
        "35C8AEEF",
        "35C8AEEE",
        "35C8AEED",
        "35C8AEEC",
        "35C8AEEB",
    ]
    assert all("/vi/tim-viec-lam/" in e.url for e in found)


async def test_glints_tail_reads_last_two_job_sitemaps() -> None:
    glints = REGISTRY["glints"]
    fetcher = FakeFetcher()
    fetcher.add("https://glints.com/sitemap_index.xml", fixture("glints", "sitemap_index.xml"))
    fetcher.add(
        "https://glints.com/sitemap_job_vn_3.xml",
        fixture("glints", "sitemap_jobs.xml"),
        content_type=XML,
    )
    found, state = await recent(glints, fetcher)
    assert urls(fetcher) == [
        "https://glints.com/sitemap_index.xml",
        "https://glints.com/sitemap_job_vn_2.xml",
        "https://glints.com/sitemap_job_vn_3.xml",
    ]
    assert len(found) >= 3
    assert not any("/en/" in e.url for e in found)
    assert all(e.lastmod is None for e in found)
    assert state.errors == ["sitemap lỗi: https://glints.com/sitemap_job_vn_2.xml"]


async def test_topdev_and_itviec_tail_read_job_sitemaps_directly() -> None:
    topdev = REGISTRY["topdev"]
    fetcher = FakeFetcher()
    page_1 = "https://topdev.vn/sitemap/jobs_desc_vi_page_1.xml"
    fetcher.add(page_1, fixture("topdev", "sitemap_jobs.xml"), content_type=XML)
    found, _ = await recent(topdev, fetcher)
    assert urls(fetcher) == [
        page_1,
        "https://topdev.vn/sitemap/jobs_desc_vi_page_2.xml",
        "https://topdev.vn/sitemap/jobs_desc_vi_page_3.xml",
    ]
    assert len(found) == 10

    itviec = REGISTRY["itviec"]
    fetcher = FakeFetcher()
    fetcher.add(
        "https://itviec.com/twinnings_jobs_desc_vn.xml",
        fixture("itviec", "sitemap_jobs.xml"),
        content_type=XML,
    )
    found, state = await recent(itviec, fetcher)
    assert len(fetcher.calls) == 1
    assert len(found) == 10
    assert all(e.lastmod is None for e in found)  # `lastmod` = giờ sinh sitemap: bỏ
    assert state.errors == []


@pytest.mark.parametrize(("board", "per_page"), [("careerlink", 50), ("vieclam24h", 30)])
async def test_listing_boards_tail_first_page(board: str, per_page: int) -> None:
    adapter = REGISTRY[board]
    assert isinstance(adapter, ListingBoard)
    html = fixture(board, "listing_1.html")
    fetcher = FakeFetcher()
    fetcher.add(adapter.listing_url.format(page=1), html)
    fetcher.add(adapter.listing_url.format(page=2), html)
    # Trang 1 đã có trong index: 1 request.
    found, _ = await recent(adapter, fetcher, known=all_known())
    assert len(found) == per_page
    assert urls(fetcher) == [adapter.listing_url.format(page=1)]
    # Toàn tin mới: đọc tiếp; trang 2 không có tin mới (trùng trang 1) thì dừng.
    fetcher.calls.clear()
    found, _ = await recent(adapter, fetcher, known=known_ids(set()))
    assert len(found) == per_page
    assert urls(fetcher) == [adapter.listing_url.format(page=p) for p in (1, 2)]
    # `limit`.
    fetcher.calls.clear()
    found, _ = await recent(adapter, fetcher, limit=7, known=known_ids(set()))
    assert len(found) == 7
    assert len(fetcher.calls) == 1


async def test_123job_tail_uses_rss_then_listing_only_when_needed() -> None:
    adapter = REGISTRY["123job"]
    rss = "https://123job.vn/rss/index"
    fetcher = FakeFetcher()
    fetcher.add_file(rss, FIXTURES / "123job" / "rss_jobs.xml")
    found, _ = await recent(adapter, fetcher, known=all_known())
    assert len(found) == 10
    assert urls(fetcher) == [rss]
    # Toàn tin mới trong RSS (tồn nhiều): đọc thêm trang danh sách đầu (ở đây lỗi → ghi lỗi,
    # vẫn dùng tin từ RSS).
    fetcher.calls.clear()
    found, state = await recent(adapter, fetcher, known=known_ids(set()))
    assert len(found) == 10
    assert urls(fetcher) == [rss, "https://123job.vn/tuyen-dung?page=1"]
    assert state.errors
    assert "tuyen-dung?page=1" in state.errors[-1]
    # RSS lỗi: đọc danh sách; danh sách cũng lỗi thì là lỗi nguồn.
    with pytest.raises(SourceError):
        await recent(adapter, FakeFetcher(), known=known_ids(set()))


async def test_vietnamworks_tail_reads_newest_api_pages() -> None:
    adapter = REGISTRY["vietnamworks"]
    data = json.loads((FIXTURES / "vietnamworks" / "search_api.json").read_text(encoding="utf-8"))
    bodies: list[dict[str, Any]] = []

    def api(kwargs: dict[str, Any]) -> FetchResult:
        bodies.append(kwargs["json"])
        return fetch_result(VNW_API, json.dumps(data), content_type="application/json")

    fetcher = FakeFetcher()
    fetcher.add_route(VNW_API, api)
    found, _ = await recent(adapter, fetcher, known=known_ids({"2120657"}))
    assert [e.external_id for e in found] == ["2120659", "2120657"]
    assert [b["page"] for b in bodies] == [0]  # nửa trang đã có: dừng
    assert bodies[0]["order"] == [{"field": "approvedOn", "value": "desc"}]
    # Toàn tin mới: thêm trang (tối đa `tail_max_pages`), trang lặp lại không thêm tin.
    bodies.clear()
    found, _ = await recent(adapter, fetcher, known=known_ids(set()))
    assert len(found) == 2
    assert [b["page"] for b in bodies] == [0, 1, 2]


def test_every_board_has_a_cheap_tail() -> None:
    """Mỗi trang có cách đọc phần mới nhất (không phải đọc cả danh sách)."""
    for adapter in REGISTRY.values():
        assert type(adapter).newest is not BoardAdapter.newest, adapter.name
        assert adapter.tail_max_pages <= 5
        tail = getattr(adapter, "tail_sitemaps", ())
        assert len(tail) <= 3, adapter.name
