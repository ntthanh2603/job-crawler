from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, text, update

from jobcrawler.config import Settings
from jobcrawler.db.models import (
    BoardIndexRun,
    Company,
    CompanyAlias,
    FailedPage,
    JobPosting,
    SourceProfile,
)
from jobcrawler.fakes import FakeChatClient, FakeFetcher, fetch_result
from jobcrawler.llm import LLMClient
from jobcrawler.net.http import FetchResult
from jobcrawler.pipeline import kyc
from jobcrawler.pipeline.board_index import BoardIndexer, IndexOptions
from jobcrawler.resources import Resources, open_resources
from jobcrawler.schemas import CompanyIn, FetchOutcome, JobStatus, KycStatus, RunStatus
from jobcrawler.services import boards as board_service
from jobcrawler.services.companies import process_company
from jobcrawler.sources import boards
from jobcrawler.text import normalize_company_name

from ..boards_fake import HOME, FakeBoard, fake_site, job_page

pytestmark = pytest.mark.integration

ACME = "Công ty Cổ phần ACME Board Việt Nam"
JOBS = {
    "viec-lam/ke-toan-11.html": ("Kế toán", ACME, "cong-ty/acme-7"),
    "viec-lam/lap-trinh-12.html": ("Lập trình viên", ACME, "cong-ty/acme-7"),
    "viec-lam/bao-ve-13.html": ("Bảo vệ", "Công ty TNHH Hoàn Toàn Khác", "cong-ty/khac-8"),
}


def settings_for(integration_settings: Settings) -> Settings:
    return integration_settings.model_copy(update={"redis_prefix": "jc-test"})


async def reset(res: Resources) -> UUID:
    """Xoá dữ liệu của trang giả, tạo doanh nghiệp ACME đã KYC xác minh."""
    async with res.sessionmaker.begin() as s:
        await s.execute(delete(JobPosting).where(JobPosting.source == FakeBoard.name))
        await s.execute(delete(SourceProfile).where(SourceProfile.source == FakeBoard.name))
        await s.execute(delete(BoardIndexRun).where(BoardIndexRun.source == FakeBoard.name))
        await s.execute(delete(Company).where(Company.input_name == ACME))
        company = Company(
            input_name=ACME,
            legal_name=ACME,
            kyc_status=KycStatus.VERIFIED.value,
            kyc_confidence=0.9,
            kyc_at=datetime.now(UTC),
        )
        s.add(company)
        await s.flush()
        # Như khi nạp danh sách: tên chuẩn hoá + khoá map profile.
        s.add_all(CompanyAlias(**row) for row in kyc.alias_rows(company.id, ACME, "input"))
        return company.id


def indexer(res: Resources, fetcher: FakeFetcher, **options: Any) -> BoardIndexer:
    return BoardIndexer(
        FakeBoard(),
        engine=res.engine,
        sessionmaker=res.sessionmaker,
        fetcher=fetcher,
        llm=LLMClient(FakeChatClient(["{}"]), model=None),
        options=IndexOptions(**options),
    )


async def postings(res: Resources) -> dict[str, JobPosting]:
    async with res.sessionmaker() as s:
        rows = await s.scalars(select(JobPosting).where(JobPosting.source == FakeBoard.name))
        return {p.external_id: p for p in rows}


async def test_incremental_index_and_profile_mapping(integration_settings: Settings) -> None:
    async with open_resources(settings_for(integration_settings), db_logs=False) as res:
        company_id = await reset(res)

        # Lượt 1: mọi tin đều mới.
        first = await indexer(res, fake_site(JOBS)).run()
        assert first.status is RunStatus.OK
        assert first.complete
        assert (first.urls_seen, first.fetched, first.new, first.failed) == (3, 3, 3, 0)
        assert (first.profiles_new, first.profiles_matched) == (2, 1)
        rows = await postings(res)
        assert set(rows) == {"11", "12", "13"}
        assert all(p.status == JobStatus.ACTIVE.value for p in rows.values())
        assert rows["11"].etag == '"viec-lam/ke-toan-11.html"'
        assert rows["11"].raw["title"] == "Kế toán"
        assert rows["11"].expires_at is not None
        async with res.sessionmaker() as s:
            profiles = {
                p.external_id: p
                for p in await s.scalars(
                    select(SourceProfile).where(SourceProfile.source == FakeBoard.name)
                )
            }
            run = await s.get(BoardIndexRun, first.run_id)
        assert profiles["7"].company_id == company_id
        assert (profiles["7"].match_evidence or {})["signal"] == "name"
        assert profiles["8"].company_id is None
        assert profiles["8"].matched_at is not None
        assert all(p.indexed_at is not None for p in profiles.values())
        assert rows["11"].source_profile_id == profiles["7"].id
        assert run is not None
        assert (run.status, run.new, run.urls_seen) == ("ok", 3, 3)

        # Lượt 2: chỉ tin 11 có lastmod mới; server trả 304 vì ETag khớp.
        site = fake_site(JOBS, lastmod={"viec-lam/ke-toan-11.html": "2099-01-01"})
        url11 = f"{HOME}viec-lam/ke-toan-11.html"

        def conditional(kwargs: dict[str, Any]) -> FetchResult:
            if (kwargs.get("headers") or {}).get("If-None-Match") == '"viec-lam/ke-toan-11.html"':
                return fetch_result(url11, status=304)
            return site.results[url11]

        site.add_route(url11, conditional)
        second = await indexer(res, site).run()
        assert (second.fetched, second.new, second.updated, second.unchanged) == (1, 0, 0, 1)
        assert (await postings(res))["11"].lastmod is not None

        # Lượt 3 và 4: tin 13 biến mất khỏi sitemap → vắng 1 lượt (còn active) → 2 lượt (removed).
        without13 = {k: v for k, v in JOBS.items() if "13" not in k}
        third = await indexer(res, fake_site(without13)).run()
        rows = await postings(res)
        assert (rows["13"].missed_runs, rows["13"].status, third.removed) == (1, "active", 0)
        fourth = await indexer(res, fake_site(without13)).run()
        rows = await postings(res)
        assert (rows["13"].status, fourth.removed) == ("removed", 1)

        # Danh sách lỗi một phần: không đánh dấu vắng mặt.
        broken = fake_site(without13)
        del broken.results[f"{HOME}sitemap-jobs-1.xml"]
        partial = await indexer(res, broken).run()
        assert partial.status is RunStatus.PARTIAL
        assert not partial.complete
        assert (await postings(res))["11"].missed_runs == 0

        # Trang chi tiết 404 khi tải lại → removed; tin gỡ thấy lại → tải lại, active.
        gone = fake_site(JOBS, lastmod={"viec-lam/lap-trinh-12.html": "2099-01-01"})
        del gone.results[f"{HOME}viec-lam/lap-trinh-12.html"]
        result = await indexer(res, gone).run()
        rows = await postings(res)
        assert rows["12"].status == "removed"
        assert rows["13"].status == "active"  # có lại trong sitemap → tải lại
        assert result.new == 0


async def profile_rows(res: Resources) -> dict[str, SourceProfile]:
    async with res.sessionmaker() as s:
        rows = await s.scalars(select(SourceProfile).where(SourceProfile.source == FakeBoard.name))
        return {p.external_id: p for p in rows}


async def test_profiles_are_fresh_only_when_all_their_jobs_are_indexed(
    integration_settings: Settings,
) -> None:
    async with open_resources(settings_for(integration_settings), db_logs=False) as res:
        await reset(res)
        # Lượt bị cắt (chỉ tải 1 trong 3 tin mới): tin chưa tải chưa biết thuộc profile nào, nên
        # không profile nào được coi là đủ tin — process_company sẽ đọc trang profile.
        first = await indexer(res, fake_site(JOBS), max_jobs=1).run()
        assert first.truncated
        assert first.profiles_fresh == 0
        assert all(p.indexed_at is None for p in (await profile_rows(res)).values())

        second = await indexer(res, fake_site(JOBS)).run()
        assert second.profiles_fresh == 2
        before = await profile_rows(res)
        assert all(p.indexed_at is not None for p in before.values())

        # Tin 13 (profile 8) đã gỡ, nay có lại trong danh sách nhưng tải lỗi: chỉ profile 8 thiếu.
        async with res.sessionmaker.begin() as s:
            await s.execute(
                update(JobPosting)
                .where(JobPosting.source == FakeBoard.name, JobPosting.external_id == "13")
                .values(status=JobStatus.REMOVED.value)
            )
        site = fake_site(JOBS)
        site.add(f"{HOME}viec-lam/bao-ve-13.html", "lỗi", status=503)
        third = await indexer(res, site).run()
        assert third.failed == 1
        after = await profile_rows(res)
        assert after["7"].indexed_at is not None
        assert before["7"].indexed_at is not None
        assert after["7"].indexed_at > before["7"].indexed_at
        assert after["8"].indexed_at == before["8"].indexed_at


async def test_rematch_maps_profiles_with_name_variants(
    integration_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dữ liệu trước khi có khoá map: alias chỉ có tên chuẩn hoá, profile lưu tên chuẩn hoá cũ.
    `rematch_profiles` thêm alias khoá map, tính lại khoá profile rồi map lại."""
    mb = "Ngân hàng TMCP Quân Đội Rematch"
    monkeypatch.setattr(boards, "registry", lambda: {FakeBoard.name: FakeBoard()})
    async with open_resources(settings_for(integration_settings), db_logs=False) as res:
        await reset(res)
        async with res.sessionmaker.begin() as s:
            await s.execute(delete(Company).where(Company.input_name == mb))
            company = Company(input_name=mb, legal_name=mb, kyc_status=KycStatus.VERIFIED.value)
            s.add(company)
            await s.flush()
            s.add(
                CompanyAlias(
                    company_id=company.id,
                    alias=mb,
                    alias_norm=normalize_company_name(mb),
                    kind="input",
                )
            )
            name = "Ngân Hàng Thương Mại Cổ Phần Quân Đội Rematch (MB Bank)"
            s.add(
                SourceProfile(
                    source=FakeBoard.name,
                    external_id="900",
                    url=f"{HOME}cong-ty/mb-900",
                    name_on_source=name,
                    name_norm=normalize_company_name(name),
                    info={},
                    matched_at=datetime.now(UTC),
                )
            )
            company_id = company.id
        report = await board_service.rematch_profiles(res, [FakeBoard.name])
        assert report.renamed >= 1
        assert report.aliases >= 1
        async with res.sessionmaker() as s:
            profile = await s.scalar(
                select(SourceProfile).where(
                    SourceProfile.source == FakeBoard.name, SourceProfile.external_id == "900"
                )
            )
        assert profile is not None
        assert profile.company_id == company_id
        assert (profile.match_evidence or {})["signal"] == "name"


async def test_refetch_without_profile_link_keeps_job_on_profile(
    integration_settings: Settings,
) -> None:
    async with open_resources(settings_for(integration_settings), db_logs=False) as res:
        await reset(res)
        await indexer(res, fake_site(JOBS)).run()
        linked = (await postings(res))["11"].source_profile_id
        assert linked is not None
        # Trang tin đổi bố cục: không còn link nào tới profile nhà tuyển dụng.
        site = fake_site(JOBS, lastmod={"viec-lam/ke-toan-11.html": "2099-01-01"})
        page = job_page("Kế toán trưởng", ACME, "cong-ty/acme-7").replace("cong-ty/acme-7", "x")
        site.add(f"{HOME}viec-lam/ke-toan-11.html", page)
        stats = await indexer(res, site).run()
        assert stats.updated == 1
        row = (await postings(res))["11"]
        assert row.raw["title"] == "Kế toán trưởng"
        assert row.source_profile_id == linked


async def test_index_run_enqueues_companies_with_new_jobs(
    integration_settings: Settings,
) -> None:
    from saq import Queue

    from .test_batch_db import drop_queue

    name = f"jobcrawler-test-{uuid4().hex[:8]}"
    settings = settings_for(integration_settings).model_copy(update={"queue_name": name})
    async with open_resources(settings, db_logs=False) as res:
        company_id = await reset(res)
        started = datetime.now(UTC)
        await indexer(res, fake_site(JOBS)).run()  # ACME (profile 7) có 2 tin mới
        queue = Queue.from_url(settings.redis_url, name=name)
        await queue.connect()
        try:
            # Doanh nghiệp vừa chạy (trong BOARD_RERUN_MIN_HOURS) thì không xếp lại.
            async with res.sessionmaker.begin() as s:
                await s.execute(
                    update(Company)
                    .where(Company.id == company_id)
                    .values(last_run_at=datetime.now(UTC))
                )
            rerun = board_service.rerun_companies_with_new_jobs
            assert await rerun(res, FakeBoard.name, started) == 0
            async with res.sessionmaker.begin() as s:
                await s.execute(
                    update(Company)
                    .where(Company.id == company_id)
                    .values(last_run_at=datetime(2020, 1, 1, tzinfo=UTC))
                )
            assert await rerun(res, FakeBoard.name, started) == 1
            # Đã có trong hàng đợi (cùng khoá doanh nghiệp): không xếp trùng.
            assert await rerun(res, FakeBoard.name, started) == 0
        finally:
            await queue.disconnect()
            await drop_queue(settings.redis_url, name)


async def test_blocked_detail_stops_run_and_lock(integration_settings: Settings) -> None:
    async with open_resources(settings_for(integration_settings), db_logs=False) as res:
        await reset(res)
        site = fake_site(JOBS)
        site.add(f"{HOME}viec-lam/ke-toan-11.html", status=403, outcome=FetchOutcome.BLOCKED)
        stats = await indexer(res, site, concurrency=1).run()
        assert stats.status is RunStatus.PARTIAL
        assert stats.error
        assert "403" in stats.error  # trang chi tiết bị chặn: dừng nguồn trong lượt này
        assert stats.truncated
        assert not stats.complete
        assert stats.fetched == 0

        # Một process khác đang index cùng trang: lượt này bỏ qua.
        async with res.engine.connect() as conn:
            await conn.execute(
                select(func.pg_advisory_lock(func.hashtext("board-index:fakeboard")))
            )
            skipped = await indexer(res, fake_site(JOBS)).run()
            await conn.execute(
                select(func.pg_advisory_unlock(func.hashtext("board-index:fakeboard")))
            )
        assert skipped.skipped
        assert skipped.run_id is None


async def test_process_company_uses_indexed_board_jobs(
    integration_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(boards, "registry", lambda: {FakeBoard.name: FakeBoard()})
    async with open_resources(settings_for(integration_settings), db_logs=False) as res:
        company_id = await reset(res)
        await indexer(res, fake_site(JOBS)).run()

        sources = await board_service.board_sources(res, company_id)
        assert [s.name for s in sources] == ["board:fakeboard:7"]
        # KYC chưa chắc: profile chỉ khớp tên thì không dùng.
        assert await board_service.board_sources(res, company_id, policy="strong") == []
        # Không tìm thấy website: profile trùng hẳn tên doanh nghiệp vẫn dùng được.
        exact = await board_service.board_sources(
            res, company_id, policy="exact_name", names=[ACME]
        )
        assert [s.name for s in exact] == ["board:fakeboard:7"]
        assert (
            await board_service.board_sources(
                res, company_id, policy="exact_name", names=["ACME Board Khác"]
            )
            == []
        )

        offline = dataclasses.replace(res, http=FakeFetcher())  # pyright: ignore[reportArgumentType]
        result = await process_company(offline, CompanyIn(name=ACME))
        assert result.company_id == company_id
        assert result.jobs_found == 2  # 2 tin của ACME từ index, không gọi mạng
        assert result.incomplete_sources == []

        # Index cũ hơn BOARD_STALE_HOURS: đọc danh sách tin trên trang profile, gộp với index.
        async with res.sessionmaker.begin() as s:
            await s.execute(
                update(SourceProfile)
                .where(SourceProfile.source == FakeBoard.name)
                .values(indexed_at=datetime(2020, 1, 1, tzinfo=UTC))
            )
        site = fake_site(JOBS)
        site.add(
            f"{HOME}cong-ty/acme-7",
            '<h1>ACME</h1><a href="/viec-lam/ke-toan-11.html">Kế toán</a>'
            '<a href="/viec-lam/thu-ky-14.html">Thư ký</a>',
        )
        site.add(f"{HOME}viec-lam/thu-ky-14.html", job_page("Thư ký", ACME, "cong-ty/acme-7"))
        crawling = dataclasses.replace(res, http=site)  # pyright: ignore[reportArgumentType]
        result = await process_company(crawling, CompanyIn(name=ACME))
        # Chưa chắc danh sách trên trang là đủ: giữ cả 2 tin đã index, chỉ tải tin mới (14).
        assert result.jobs_found == 3
        assert [u for _, u, _ in site.calls] == [
            f"{HOME}cong-ty/acme-7",
            f"{HOME}viec-lam/thu-ky-14.html",
        ]

        # Adapter đã kiểm phân trang (danh sách đủ): tin index không còn trên trang là đã đóng.
        class PagedBoard(FakeBoard):
            profile_listing_complete = True

        monkeypatch.setattr(boards, "registry", lambda: {FakeBoard.name: PagedBoard()})
        site = fake_site(JOBS)
        site.add(
            f"{HOME}cong-ty/acme-7",
            '<h1>ACME</h1><a href="/viec-lam/ke-toan-11.html">Kế toán</a>',
        )
        crawling = dataclasses.replace(res, http=site)  # pyright: ignore[reportArgumentType]
        result = await process_company(crawling, CompanyIn(name=ACME))
        assert result.jobs_found == 1
        assert [u for _, u, _ in site.calls] == [f"{HOME}cong-ty/acme-7"]


async def test_serp_lookup_adopts_profile_on_board(
    integration_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chưa có profile trên trang: tìm bằng SERP `site:`, mở trang profile, map bằng rule."""
    from pydantic import SecretStr

    from jobcrawler.serp import SERPER_URL

    monkeypatch.setattr(boards, "registry", lambda: {FakeBoard.name: FakeBoard()})
    settings = settings_for(integration_settings).model_copy(
        update={"serp_api_key": SecretStr("test-key"), "serp_cache_seconds": 0}
    )
    async with open_resources(settings, db_logs=False) as res:
        company_id = await reset(res)
        site = fake_site(JOBS)
        queries: list[str] = []

        def serper(kwargs: dict[str, Any]) -> FetchResult:
            queries.append(kwargs["json"]["q"])
            body = {
                "organic": [
                    {"title": "Tin tức", "link": "https://news.test/acme"},
                    {"title": ACME, "link": f"{HOME}cong-ty/acme-7"},
                    {"title": "Công ty khác", "link": f"{HOME}cong-ty/khac-8"},
                ]
            }
            return fetch_result(SERPER_URL, json.dumps(body), content_type="application/json")

        site.add_route(SERPER_URL, serper)
        site.add(
            f"{HOME}cong-ty/acme-7",
            f'<h1>{ACME}</h1><a href="/viec-lam/ke-toan-11.html">Kế toán</a>',
        )
        site.add(f"{HOME}cong-ty/khac-8", "<h1>Công ty TNHH Hoàn Toàn Khác</h1>")
        online = dataclasses.replace(res, http=site)  # pyright: ignore[reportArgumentType]
        found = await board_service.lookup_profiles(
            online, await _company(res, company_id), timeout=10, records=[]
        )
        assert found == 1
        assert queries == [f'"{ACME}" (site:board.test)']
        async with res.sessionmaker() as s:
            profiles = (
                await s.scalars(select(SourceProfile).where(SourceProfile.source == FakeBoard.name))
            ).all()
        [profile] = profiles
        assert (profile.external_id, profile.company_id) == ("7", company_id)
        assert (profile.match_evidence or {})["via"] == "serp"
        # Đã có profile trên mọi trang: không tìm nữa.
        again = await board_service.lookup_profiles(
            online, await _company(res, company_id), timeout=10, records=[]
        )
        assert again == 0
        assert len(queries) == 1


async def _company(res: Resources, company_id: UUID) -> Company:
    async with res.sessionmaker() as s:
        company = await s.get(Company, company_id)
        assert company is not None
        return company


async def test_unparseable_detail_page_is_kept_in_failed_pages(
    integration_settings: Settings,
) -> None:
    async with open_resources(settings_for(integration_settings), db_logs=False) as res:
        await reset(res)
        async with res.sessionmaker.begin() as s:
            await s.execute(delete(FailedPage).where(FailedPage.source == FakeBoard().source))
        site = fake_site(JOBS)
        broken = f"{HOME}viec-lam/bao-ve-13.html"
        site.add(broken, "<html><body><h1>Bố cục mới</h1>" + "<p>không có JSON-LD</p>" * 50)
        stats = await indexer(res, site).run()
        assert stats.failed == 1
        assert stats.new == 2
        async with res.sessionmaker() as s:
            [page] = (
                await s.scalars(select(FailedPage).where(FailedPage.source == FakeBoard().source))
            ).all()
            stored = await s.scalar(
                select(func.octet_length(text("html_zstd")))
                .select_from(FailedPage)
                .where(FailedPage.id == page.id)
            )
        assert page.url == broken
        assert page.reason.startswith("no_job")
        assert page.http_status == 200
        assert page.html is not None
        assert page.html.startswith("<html><body><h1>Bố cục mới</h1>")
        assert stored is not None
        assert stored < len(page.html.encode()) / 5  # HTML lưu nén
