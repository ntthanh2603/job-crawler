"""Lượt index đuôi (ban ngày) trên Postgres thật: chỉ thêm tin chưa có, không đánh dấu gỡ, không
đặt `indexed_at`; doanh nghiệp có tin mới để runner poll; poll không gọi trang tuyển dụng."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import delete, func, select, update

from jobcrawler.common.config import Settings
from jobcrawler.common.schemas import JobStatus, KycStatus, RunStatus
from jobcrawler.database.models import (
    BoardIndexRun,
    Company,
    CompanyAlias,
    JobPosting,
    SourceProfile,
)
from jobcrawler.modules.boards import boards_service
from jobcrawler.pipeline import kyc
from jobcrawler.pipeline.board_index import BoardIndexer, IndexOptions
from jobcrawler.services.fakes import FakeChatClient, FakeFetcher, fetch_result
from jobcrawler.services.llm import LLMClient
from jobcrawler.services.net.http import FetchResult
from jobcrawler.services.resources import Resources, open_resources
from jobcrawler.sources import boards
from jobcrawler.sources.base import SourceSession
from jobcrawler.sources.boards.base import BoardAdapter

from ..boards_fake import HOME, FakeBoard, fake_site, urlset
from .test_board_index_db import ACME, JOBS, postings, profile_rows, reset, settings_for

pytestmark = pytest.mark.integration

NEW_EMPLOYER = "Công ty TNHH Đuôi Mới Việt Nam"
TAIL_JOBS = {
    **{k: v for k, v in JOBS.items() if "13" not in k},  # tin 13 không còn trong phần đuôi
    "viec-lam/thu-ky-14.html": ("Thư ký", ACME, "cong-ty/acme-7"),
    "viec-lam/tai-xe-15.html": ("Tài xế", NEW_EMPLOYER, "cong-ty/duoi-moi-9"),
}


def tail_indexer(
    res: Resources, fetcher: FakeFetcher, adapter: BoardAdapter | None = None, **options: Any
) -> BoardIndexer:
    return BoardIndexer(
        adapter or FakeBoard(),
        engine=res.engine,
        sessionmaker=res.sessionmaker,
        fetcher=fetcher,
        llm=LLMClient(FakeChatClient(["{}"]), model=None),
        options=IndexOptions(tail=True, **options),
    )


def full_indexer(res: Resources, fetcher: FakeFetcher) -> BoardIndexer:
    return BoardIndexer(
        FakeBoard(),
        engine=res.engine,
        sessionmaker=res.sessionmaker,
        fetcher=fetcher,
        llm=LLMClient(FakeChatClient(["{}"]), model=None),
    )


def detail_calls(fetcher: FakeFetcher) -> list[str]:
    return sorted(u for _, u, _ in fetcher.calls if "/viec-lam/" in u)


async def add_company(res: Resources, name: str) -> Any:
    async with res.sessionmaker.begin() as s:
        await s.execute(delete(Company).where(Company.input_name == name))
        company = Company(
            input_name=name,
            legal_name=name,
            kyc_status=KycStatus.VERIFIED.value,
            kyc_confidence=0.9,
            kyc_at=datetime.now(UTC),
        )
        s.add(company)
        await s.flush()
        s.add_all(CompanyAlias(**row) for row in kyc.alias_rows(company.id, name, "input"))
        return company.id


async def test_tail_adds_only_new_postings_and_reports_companies(
    integration_settings: Settings,
) -> None:
    async with open_resources(settings_for(integration_settings), db_logs=False) as res:
        acme_id = await reset(res)
        await full_indexer(res, fake_site(JOBS)).run()
        new_id = await add_company(res, NEW_EMPLOYER)
        before = await profile_rows(res)
        # Tin 12 đã gỡ (lượt đủ), nay lại có trong phần đuôi: lượt đuôi không tải lại.
        async with res.sessionmaker.begin() as s:
            await s.execute(
                update(JobPosting)
                .where(JobPosting.source == FakeBoard.name, JobPosting.external_id == "12")
                .values(status=JobStatus.REMOVED.value)
            )
        since = datetime.now(UTC)
        assert await boards_service.companies_with_new_postings(res, FakeBoard.name, since) == []

        # Tin 11 có `lastmod` mới: lượt đuôi cũng không tải lại (để lượt đủ ban đêm).
        site = fake_site(TAIL_JOBS, lastmod={"viec-lam/ke-toan-11.html": "2099-01-01"})
        stats = await tail_indexer(res, site).run()
        assert stats.mode == "tail"
        assert stats.status is RunStatus.OK
        assert not stats.complete
        assert (stats.urls_seen, stats.to_fetch, stats.fetched, stats.new) == (4, 2, 2, 2)
        assert (stats.removed, stats.profiles_new, stats.profiles_matched) == (0, 1, 1)
        assert detail_calls(site) == [
            f"{HOME}viec-lam/tai-xe-15.html",
            f"{HOME}viec-lam/thu-ky-14.html",
        ]

        rows = await postings(res)
        assert set(rows) == {"11", "12", "13", "14", "15"}
        # Không có trong phần đuôi ≠ đã gỡ.
        assert (rows["13"].status, rows["13"].missed_runs) == (JobStatus.ACTIVE.value, 0)
        assert rows["12"].status == JobStatus.REMOVED.value
        after = await profile_rows(res)
        assert rows["14"].source_profile_id == after["7"].id
        # Phần đuôi không chứng minh profile đủ tin: `indexed_at` giữ nguyên / chưa đặt.
        assert after["7"].indexed_at == before["7"].indexed_at
        assert after["9"].indexed_at is None
        # Profile mới được map ngay trong lượt đuôi.
        assert after["9"].company_id == new_id

        async with res.sessionmaker() as s:
            run = await s.get(BoardIndexRun, stats.run_id)
        assert run is not None
        assert (run.status, run.complete, run.new, run.removed) == ("ok", False, 2, 0)
        assert run.stats["mode"] == "tail"

        # Runner poll ngay: ACME (tin 14) và doanh nghiệp vừa map (tin 15).
        found = await boards_service.companies_with_new_postings(res, FakeBoard.name, since)
        assert sorted(found) == sorted([acme_id, new_id])

        # Lượt đuôi sau: không còn tin mới → không tải trang chi tiết nào.
        later = datetime.now(UTC)
        again_site = fake_site(TAIL_JOBS)
        again = await tail_indexer(res, again_site).run()
        assert (again.urls_seen, again.fetched, again.new) == (4, 0, 0)
        assert detail_calls(again_site) == []
        assert await boards_service.companies_with_new_postings(res, FakeBoard.name, later) == []


async def test_tail_truncated_by_max_jobs_catches_up_next_run(
    integration_settings: Settings,
) -> None:
    async with open_resources(settings_for(integration_settings), db_logs=False) as res:
        await reset(res)
        first = await tail_indexer(res, fake_site(JOBS), max_jobs=1).run()
        assert (first.to_fetch, first.fetched, first.truncated) == (3, 1, True)
        assert first.status is RunStatus.PARTIAL
        second = await tail_indexer(res, fake_site(JOBS)).run()
        assert (second.to_fetch, second.fetched, second.new) == (2, 2, 2)
        assert second.status is RunStatus.OK
        assert set(await postings(res)) == {"11", "12", "13"}


async def test_tail_skipped_while_full_index_holds_lock(integration_settings: Settings) -> None:
    async with open_resources(settings_for(integration_settings), db_logs=False) as res:
        await reset(res)
        site = fake_site(JOBS)
        async with res.engine.connect() as conn:
            key = func.hashtext("board-index:fakeboard")
            await conn.execute(select(func.pg_advisory_lock(key)))
            try:
                skipped = await tail_indexer(res, site).run()
            finally:
                await conn.execute(select(func.pg_advisory_unlock(key)))
        assert skipped.skipped
        assert skipped.mode == "tail"
        assert skipped.run_id is None
        assert site.calls == []  # không chờ khoá, không gọi mạng
        async with res.sessionmaker() as s:
            runs = await s.scalar(
                select(func.count())
                .select_from(BoardIndexRun)
                .where(BoardIndexRun.source == FakeBoard.name)
            )
        assert runs == 0


class ConditionalBoard(FakeBoard):
    tail_sitemaps = (f"{HOME}sitemap-jobs-1.xml",)
    tail_conditional = True


async def test_tail_asks_if_modified_since_after_ok_run(integration_settings: Settings) -> None:
    async with open_resources(settings_for(integration_settings), db_logs=False) as res:
        await reset(res)
        site = fake_site(JOBS)
        sitemap = f"{HOME}sitemap-jobs-1.xml"
        body = urlset(*((path, None) for path in JOBS))
        asked: list[str | None] = []

        def conditional(kwargs: dict[str, Any]) -> FetchResult:
            since = (kwargs.get("headers") or {}).get("If-Modified-Since")
            asked.append(since)
            if since:
                return fetch_result(sitemap, status=304)
            return fetch_result(sitemap, body, content_type="application/xml")

        site.add_route(sitemap, conditional)
        first = await tail_indexer(res, site, ConditionalBoard()).run()
        assert (first.status, first.new) == (RunStatus.OK, 3)
        second = await tail_indexer(res, site, ConditionalBoard()).run()
        # Lượt OK trước đó: hỏi "có đổi không"; 304 = không có tin mới, không phải lỗi.
        assert asked[0] is None
        assert asked[1]
        assert asked[1].endswith(" GMT")
        assert (second.status, second.urls_seen, second.fetched) == (RunStatus.OK, 0, 0)


async def test_offline_board_sources_never_fetch_stale_profile(
    integration_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(boards, "registry", lambda: {FakeBoard.name: FakeBoard()})
    async with open_resources(settings_for(integration_settings), db_logs=False) as res:
        company_id = await reset(res)
        await full_indexer(res, fake_site(JOBS)).run()
        # Index cũ hơn BOARD_STALE_HOURS: lượt build sẽ đọc trang profile, lượt poll thì không.
        async with res.sessionmaker.begin() as s:
            await s.execute(
                update(SourceProfile)
                .where(SourceProfile.source == FakeBoard.name)
                .values(indexed_at=datetime(2020, 1, 1, tzinfo=UTC))
            )
        sources = await boards_service.board_sources(res, company_id, network=False)
        assert [s.name for s in sources] == ["board:fakeboard:7"]
        quiet = FakeFetcher()
        session = SourceSession(
            "board:fakeboard", quiet, LLMClient(FakeChatClient(["{}"]), model=None)
        )
        jobs = [job async for job in sources[0].jobs(session)]
        assert sorted(j.external_id for j in jobs) == ["11", "12"]
        assert quiet.calls == []
