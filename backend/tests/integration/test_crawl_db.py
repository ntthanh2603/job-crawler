"""Vòng đời build → poll của một doanh nghiệp trên site careers giả (FakeFetcher + Postgres +
Redis thật): bộ khung được ghi, poll không đổi thì không ghi gì, tin mới chỉ mở đúng trang của
nó, tin gỡ thì việc bị đóng, trang chính hỏng thì bộ khung `broken` và doanh nghiệp được build
lại."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select, update

from jobcrawler.common.config import Settings
from jobcrawler.common.schemas import (
    JobLevel,
    JobStatus,
    KycStatus,
    PlanStatus,
    RunKind,
    RunTrigger,
    ScheduleState,
)
from jobcrawler.database import fencing
from jobcrawler.database.models import (
    Company,
    CompanyRun,
    CompanySchedule,
    CrawlPlan,
    Job,
    JobPosting,
)
from jobcrawler.modules.crawl import crawl_service, schedule_service, sla_service
from jobcrawler.pipeline import dedup, store
from jobcrawler.pipeline.normalize import NormalizedJob
from jobcrawler.pipeline.verify import Verdict
from jobcrawler.services.fakes import FakeFetcher
from jobcrawler.services.redis.queues import drop_queue
from jobcrawler.services.resources import Resources, open_resources
from jobcrawler.sources import boards

from ..jobs_fixtures import NOW, normalized, raw_job

pytestmark = pytest.mark.integration

SITE = "https://acme-poll.vn"
LISTING = f"{SITE}/tuyen-dung"
NAME = "Công ty TNHH Acme Poll"
JD = (
    "Mô tả công việc: phát triển và vận hành hệ thống quản lý kho cho các cửa hàng của công ty, "
    "viết mã Python bất đồng bộ, thiết kế cơ sở dữ liệu PostgreSQL. Yêu cầu: ít nhất "
    "hai năm kinh nghiệm, hiểu HTTP, biết đọc tài liệu tiếng Anh. Quyền lợi: lương tháng 13, bảo "
    "hiểm đầy đủ, du lịch hằng năm, làm việc lai từ xa hai ngày mỗi tuần."
)


def job_path(slug: str, job_id: int) -> str:
    return f"{LISTING}/{slug}-{job_id}"


def detail_html(title: str) -> str:
    posting = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": title,
        "description": f"<p>{JD}</p><p>Vị trí: {title}.</p>",
        "datePosted": "2026-10-15",
        "validThrough": "2027-12-31",
        "employmentType": "FULL_TIME",
        "hiringOrganization": {"@type": "Organization", "name": NAME},
        "jobLocation": {
            "@type": "Place",
            "address": {"@type": "PostalAddress", "addressLocality": "Hà Nội"},
        },
    }
    return (
        f"<html><head><title>{title}</title>"
        f'<script type="application/ld+json">{json.dumps(posting, ensure_ascii=False)}</script>'
        f"</head><body><main><h1>{title}</h1><p>{JD}</p></main></body></html>"
    )


class Site:
    """Site careers giả: trang chủ → /tuyen-dung → trang từng tin (JSON-LD)."""

    def __init__(self) -> None:
        self.fetcher = FakeFetcher()
        self.jobs: dict[str, str] = {}  # URL → tiêu đề
        self.fetcher.add(
            f"{SITE}/",
            f'<html><body><nav><a href="{LISTING}">Tuyển dụng</a></nav><p>Acme</p></body></html>',
            content_type="text/html; charset=utf-8",
        )

    def publish(self, slug: str, job_id: int, title: str) -> str:
        url = job_path(slug, job_id)
        self.jobs[url] = title
        self.fetcher.add(url, detail_html(title), content_type="text/html; charset=utf-8")
        self._listing()
        return url

    def unpublish(self, url: str) -> None:
        del self.jobs[url]
        self._listing()

    def _listing(self, html: str | None = None) -> None:
        links = "".join(f'<li><a href="{u}">{t}</a></li>' for u, t in self.jobs.items())
        body = html or f"<html><body><h1>Cơ hội nghề nghiệp</h1><ul>{links}</ul></body></html>"
        self.fetcher.add(LISTING, body, content_type="text/html; charset=utf-8")

    def break_listing(self, status: int) -> None:
        self.fetcher.results.pop(LISTING, None)
        if status != 404:
            self._listing("<html><body><h1>Trang đang bảo trì</h1></body></html>")

    def opened(self, url: str) -> int:
        return [u for _, u, _ in self.fetcher.calls].count(url)


@pytest.fixture
async def env(
    integration_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[Resources, Site, UUID]]:
    monkeypatch.setattr(boards, "registry", dict)  # không có trang tuyển dụng
    tag = uuid4().hex[:8]
    settings = integration_settings.model_copy(
        update={
            "redis_prefix": f"jc-test-{tag}",
            "build_queue_name": f"jc-test-build-{tag}",
            "poll_queue_name": f"jc-test-poll-{tag}",
            "board_queue_name": f"jc-test-boards-{tag}",
            "llm_base_url": None,
            "serp_api_key": None,
            "poll_window": "00:00-00:00",  # cả ngày
            "build_refresh_discovery": True,
        }
    )
    site = Site()
    site.publish("ky-su-phan-mem-python", 101, "Kỹ sư phần mềm Python")
    site.publish("ke-toan-tong-hop", 102, "Kế toán tổng hợp")
    async with open_resources(settings, db_logs=False) as res:
        async with res.sessionmaker.begin() as s:
            await s.execute(delete(Company).where(Company.input_name == NAME))
            company = Company(
                input_name=NAME,
                legal_name=NAME,
                input_website=SITE,
                primary_domain="acme-poll.vn",
                kyc_status=KycStatus.VERIFIED.value,
                kyc_confidence=0.95,
                kyc_at=datetime.now(UTC),
            )
            s.add(company)
            await s.flush()
            company_id = company.id
        live = dataclasses.replace(res, http=site.fetcher)  # pyright: ignore[reportArgumentType]
        try:
            yield live, site, company_id
        finally:
            for name in settings.queue_names:
                await drop_queue(settings.redis_url, name)
            async for key in res.redis.scan_iter(match=f"{settings.redis_prefix}:*"):
                await res.redis.delete(key)


async def active_titles(res: Resources, company_id: UUID) -> list[str]:
    async with res.sessionmaker() as s:
        rows = await s.scalars(
            select(Job.title).where(
                Job.company_id == company_id, Job.status == JobStatus.ACTIVE.value
            )
        )
        return sorted(rows)


async def runs(res: Resources, company_id: UUID, kind: RunKind) -> int:
    async with res.sessionmaker() as s:
        return int(
            await s.scalar(
                select(func.count())
                .select_from(CompanyRun)
                .where(CompanyRun.company_id == company_id, CompanyRun.kind == kind.value)
            )
            or 0
        )


async def test_build_then_poll_lifecycle(env: tuple[Resources, Site, UUID]) -> None:
    res, site, company_id = env

    # 1. Build: tìm nguồn, đọc đủ, ghi bộ khung + lịch.
    built = await crawl_service.build_company(res, company_id, trigger=RunTrigger.NEW)
    assert built.jobs_accepted == 2, built
    assert await active_titles(res, company_id) == ["Kế toán tổng hợp", "Kỹ sư phần mềm Python"]
    async with res.sessionmaker() as s:
        [plan] = (
            await s.scalars(select(CrawlPlan).where(CrawlPlan.company_id == company_id))
        ).all()
        schedule = await s.get(CompanySchedule, company_id)
    assert plan.status == PlanStatus.ACTIVE.value
    assert plan.spec["listings"] == [LISTING]
    assert plan.jobs == 2
    assert len(plan.job_keys) == 2
    assert schedule is not None
    assert schedule.state == ScheduleState.READY.value
    assert schedule.next_poll_at is not None
    assert schedule.next_build_at is not None

    # 2. Poll không đổi: chỉ đọc trang danh sách, không mở trang tin, không ghi lượt chạy.
    calls = len(site.fetcher.calls)
    quiet = await crawl_service.poll_company(res, company_id)
    assert (quiet.status, quiet.new, quiet.gone, quiet.saved) == ("ok", 0, 0, False)
    assert [u for _, u, _ in site.fetcher.calls[calls:]] == [LISTING]
    assert site.fetcher.calls[-1][2].get("use_cache") is False, site.fetcher.calls[-1]
    assert await runs(res, company_id, RunKind.POLL) == 0

    # 3. Tin mới: chỉ mở trang của tin đó, lưu, ghi một lượt poll.
    new_url = site.publish("chuyen-vien-tuyen-dung", 103, "Chuyên viên tuyển dụng")
    fresh = await crawl_service.poll_company(res, company_id)
    assert (fresh.new, fresh.saved) == (1, True), fresh
    assert site.opened(new_url) == 1
    assert site.opened(job_path("ke-toan-tong-hop", 102)) == 1  # vẫn chỉ lần mở ở lượt build
    assert "Chuyên viên tuyển dụng" in await active_titles(res, company_id)
    assert await runs(res, company_id, RunKind.POLL) == 1
    # Trang "Bản đồ bộ khung": mỗi mã tin ghép được với tin đã lưu (theo loại nguồn + mã).
    async with res.sessionmaker() as s:
        found = await crawl_service.plan_detail(s, plan.id)
    assert found is not None
    _, _, postings = found
    assert sorted(p.title or "" for p in postings.values()) == [
        "Chuyên viên tuyển dụng",
        "Kế toán tổng hợp",
        "Kỹ sư phần mềm Python",
    ]
    assert new_url in {p.url for p in postings.values()}

    # 4. Tin bị gỡ khỏi danh sách: việc bị đóng.
    site.unpublish(job_path("ke-toan-tong-hop", 102))
    gone = await crawl_service.poll_company(res, company_id)
    assert (gone.gone, gone.saved, gone.closed) == (1, True, 1), gone
    assert "Kế toán tổng hợp" not in await active_titles(res, company_id)

    # 5. Trang danh sách trả 404: bộ khung hỏng, doanh nghiệp được xếp build lại.
    site.break_listing(404)
    broken = await crawl_service.poll_company(res, company_id)
    assert broken.status == "broken"
    assert broken.rebuild
    async with res.sessionmaker() as s:
        plan_after = await s.get(CrawlPlan, plan.id)
        schedule = await s.get(CompanySchedule, company_id)
    assert plan_after is not None
    assert plan_after.status == PlanStatus.BROKEN.value
    assert schedule is not None
    assert schedule.state == ScheduleState.BROKEN.value
    job = await res.queues.build.job(crawl_service.build_key(company_id))
    assert job is not None
    assert job.kwargs["trigger"] == RunTrigger.ERROR.value
    # Việc đang mở không bị đóng vì trang hỏng.
    assert len(await active_titles(res, company_id)) == 2

    stats = await sla_service.poll_stats(res.redis, res.settings.redis_prefix, minutes=5)
    assert stats.polls == 4
    assert stats.ok == 3
    assert stats.broken == 1


async def test_empty_listing_marks_plan_broken(env: tuple[Resources, Site, UUID]) -> None:
    res, site, company_id = env
    await crawl_service.build_company(res, company_id, trigger=RunTrigger.NEW)
    site.break_listing(200)  # trang mở được nhưng không còn link tin nào
    outcome = await crawl_service.poll_company(res, company_id)
    assert outcome.status == "broken"
    assert "empty_listing" in outcome.sources[f"careers:{SITE.removeprefix('https://')}"]
    assert len(await active_titles(res, company_id)) == 2


async def test_poll_skips_while_company_is_building(env: tuple[Resources, Site, UUID]) -> None:
    res, _site, company_id = env
    async with crawl_service.company_lock(res, company_id, ttl=30) as held:
        assert held
        outcome = await crawl_service.poll_company(res, company_id)
    assert outcome.status == "skipped"


async def test_company_lock_fences_writes_of_stale_holder(
    env: tuple[Resources, Site, UUID],
) -> None:
    res, _site, company_id = env

    async def write(error: str) -> None:
        async with res.sessionmaker.begin() as s:
            await s.execute(update(Company).where(Company.id == company_id).values(note=error))

    async def last_error() -> str | None:
        async with res.sessionmaker() as s:
            row = await s.get(Company, company_id)
            return row.note if row else None

    async with crawl_service.company_lock(res, company_id, ttl=30) as held:
        assert held
        fence = fencing.current()
        assert fence is not None
        old_token = fence[1]
        await write("lượt cũ")  # còn giữ khoá: ghi được
    # Lượt mới giữ khoá (token tăng): lượt cũ (process đứng quá hạn khoá rồi chạy tiếp) không ghi
    # được nữa, kể cả từ task con.
    new_token = await fencing.next_token(res.sessionmaker, company_id)
    assert new_token == old_token + 1
    with fencing.fenced(company_id, old_token):
        with pytest.raises(fencing.FencedOut):
            await write("lượt cũ ghi đè")
        with pytest.raises(fencing.FencedOut):
            await asyncio.create_task(write("task con của lượt cũ"))
    with fencing.fenced(company_id, new_token):
        await write("lượt mới")
    assert await last_error() == "lượt mới"
    # Không trong khối `fenced`: không kiểm (API, CLI...).
    await write("ngoài khoá")
    assert await last_error() == "ngoài khoá"
    # Doanh nghiệp không tồn tại: không có token, không chặn.
    assert await fencing.next_token(res.sessionmaker, uuid4()) is None


async def test_scheduler_tick_builds_new_and_polls_ready(
    env: tuple[Resources, Site, UUID],
) -> None:
    from jobcrawler.modules.crawl import schedule_service

    res, _site, company_id = env
    now = datetime.now(UTC)
    first = await schedule_service.tick(res, now)
    assert first["builds"] >= 1
    assert await res.queues.build.job(crawl_service.build_key(company_id)) is not None
    await crawl_service.build_company(res, company_id, trigger=RunTrigger.NEW)
    async with res.sessionmaker.begin() as s:
        row = await s.get(CompanySchedule, company_id)
        assert row is not None
        row.next_poll_at = now  # đến hạn ngay
    second = await schedule_service.tick(res, datetime.now(UTC))
    assert second["polls"] >= 1
    assert await res.queues.poll.job(crawl_service.poll_key(company_id)) is not None
    async with res.sessionmaker() as s:
        row = await s.get(CompanySchedule, company_id)
    assert row is not None
    assert row.next_poll_at is not None
    assert row.next_poll_at > now


async def test_mass_disappearance_is_not_closed_until_a_rebuild(
    env: tuple[Resources, Site, UUID],
) -> None:
    """Một lượt poll thấy mất nhiều tin cùng lúc: không đóng việc ngay (có thể bộ khung thiếu
    nguồn dữ liệu), lặp lại đủ POLL_MAX_FAILURES lượt thì build lại để biết chắc."""
    res, site, company_id = env
    titles = [
        "Chuyên viên pháp chế",
        "Nhân viên thu mua",
        "Kỹ sư dữ liệu",
        "Trưởng phòng hành chính",
    ]
    extra = [site.publish(f"vi-tri-{i}", 200 + i, title) for i, title in enumerate(titles)]
    await crawl_service.build_company(res, company_id, trigger=RunTrigger.NEW)
    assert len(await active_titles(res, company_id)) == 6
    for url in extra[:3]:
        site.unpublish(url)
    for attempt in range(1, res.settings.poll_max_failures + 1):
        outcome = await crawl_service.poll_company(res, company_id)
        assert outcome.gone == 0, outcome
        assert len(await active_titles(res, company_id)) == 6, outcome  # chưa đóng việc nào
        if attempt < res.settings.poll_max_failures:
            assert outcome.status == "ok"
            assert "coverage_drop" in next(iter(outcome.sources.values()))
    assert outcome.status == "broken"
    job = await res.queues.build.job(crawl_service.build_key(company_id))
    assert job is not None
    # Lượt build lại đọc đủ: đóng đúng 3 việc đã gỡ.
    await res.queues.build.drop()
    await res.queues.build.connect()
    await crawl_service.build_company(res, company_id, trigger=RunTrigger.ERROR)
    assert len(await active_titles(res, company_id)) == 3


async def test_truncated_poll_remembers_new_jobs(env: tuple[Resources, Site, UUID]) -> None:
    """Lượt poll không đọc trọn (chạm POLL_MAX_DETAIL_PAGES) không đóng việc nào, nhưng tin mới đã
    xử lý được ghi vào tập mã — lượt sau không coi là mới và xử lý lại."""
    res, site, company_id = env
    await crawl_service.build_company(res, company_id, trigger=RunTrigger.NEW)
    first_url = site.publish("chuyen-vien-tuyen-dung", 103, "Chuyên viên tuyển dụng")
    second_url = site.publish("nhan-vien-kho-van", 104, "Nhân viên kho vận")
    res.settings = res.settings.model_copy(update={"poll_max_detail_pages": 1})

    first = await crawl_service.poll_company(res, company_id)
    assert (first.new, first.gone, first.saved) == (1, 0, True), first
    assert (site.opened(first_url), site.opened(second_url)) == (1, 0)
    async with res.sessionmaker() as s:
        [plan] = (
            await s.scalars(select(CrawlPlan).where(CrawlPlan.company_id == company_id))
        ).all()
    assert len(plan.job_keys) == 3  # 2 mã cũ + mã vừa xử lý; tin chưa mở không được ghi

    second = await crawl_service.poll_company(res, company_id)
    assert (second.new, second.saved) == (1, True), second  # chỉ tin còn lại
    assert (site.opened(first_url), site.opened(second_url)) == (1, 1)
    third = await crawl_service.poll_company(res, company_id)
    assert (third.new, third.saved) == (0, False), third
    assert len(await active_titles(res, company_id)) == 4


async def test_rebuild_on_error_off_waits_for_the_nightly_build(
    env: tuple[Resources, Site, UUID],
) -> None:
    """REBUILD_ON_ERROR tắt: bộ khung hỏng không build lại ban ngày, chờ mốc build đêm; doanh
    nghiệp vẫn `ready` (nguồn khác vẫn được poll)."""
    res, site, company_id = env
    await crawl_service.build_company(res, company_id, trigger=RunTrigger.NEW)
    res.settings = res.settings.model_copy(update={"rebuild_on_error": False})
    site.break_listing(404)
    started = datetime.now(UTC)
    outcome = await crawl_service.poll_company(res, company_id)
    assert (outcome.status, outcome.rebuild) == ("broken", False)
    async with res.sessionmaker() as s:
        schedule = await s.get(CompanySchedule, company_id)
    assert schedule is not None
    assert schedule.state == ScheduleState.READY.value
    assert schedule.next_build_at is not None
    assert schedule.next_build_at > started
    assert schedule.next_build_at <= schedule_service.next_build_at(
        company_id, datetime.now(UTC), res.settings
    )
    assert await res.queues.build.job(crawl_service.build_key(company_id)) is None


async def test_poll_never_unpauses_a_paused_company(env: tuple[Resources, Site, UUID]) -> None:
    res, site, company_id = env
    await crawl_service.build_company(res, company_id, trigger=RunTrigger.NEW)
    async with res.sessionmaker.begin() as s:
        await s.execute(
            update(CompanySchedule)
            .where(CompanySchedule.company_id == company_id)
            .values(state=ScheduleState.PAUSED.value)
        )
    # Trang tuyển dụng có tin mới của doanh nghiệp đã tắt thu thập: bỏ lượt.
    board = await crawl_service.poll_company(res, company_id, trigger=RunTrigger.BOARD)
    assert board.status == "skipped"
    # Người dùng bấm poll, bộ khung hỏng: không build lại, lịch vẫn `paused`.
    site.break_listing(404)
    manual = await crawl_service.poll_company(res, company_id, trigger=RunTrigger.MANUAL)
    assert (manual.status, manual.rebuild) == ("broken", False)
    async with res.sessionmaker() as s:
        schedule = await s.get(CompanySchedule, company_id)
    assert schedule is not None
    assert schedule.state == ScheduleState.PAUSED.value
    assert await res.queues.build.job(crawl_service.build_key(company_id)) is None


async def test_board_poll_deduped_by_a_waiting_poll_is_not_lost(
    env: tuple[Resources, Site, UUID],
) -> None:
    """Lượt poll `board` trùng khoá `poll:<id>` của lượt theo lịch đang chờ: lượt đó đọc cờ và đọc
    tin trang tuyển dụng, tin không phải chờ tới build đêm."""
    res, _site, company_id = env
    await crawl_service.build_company(res, company_id, trigger=RunTrigger.NEW)
    assert await crawl_service.enqueue_polls(res, [company_id], trigger=RunTrigger.SCHEDULE) == 1
    assert await crawl_service.enqueue_polls(res, [company_id], trigger=RunTrigger.BOARD) == 0
    outcome = await crawl_service.poll_company(res, company_id, trigger=RunTrigger.SCHEDULE)
    assert outcome.saved, outcome
    async with res.sessionmaker() as s:
        triggers = (
            await s.scalars(
                select(CompanyRun.trigger).where(
                    CompanyRun.company_id == company_id, CompanyRun.kind == RunKind.POLL.value
                )
            )
        ).all()
    assert list(triggers) == [RunTrigger.BOARD.value]
    assert not await crawl_service.take_board_pending(res, company_id)


async def test_partial_run_merges_with_the_jobs_other_postings(
    integration_settings: Settings,
) -> None:
    """Lượt chưa đủ chỉ thấy tin TopCV của một việc có bản chuẩn là tin careers: việc giữ bản chuẩn
    careers, mọi URL nguồn, lương / hạn nộp của tin careers (không đổi qua lại giữa ngày và đêm).
    Tin careers đã gỡ (`absent`) thì không gộp lại; tin đăng lại với mã mới gộp với tin TopCV."""
    async with open_resources(integration_settings, db_logs=False) as res:
        async with res.sessionmaker.begin() as s:
            company = Company(input_name=f"ACME partial {uuid4().hex[:6]}")
            s.add(company)
            await s.flush()
            company_id = company.id
        suffix = uuid4().hex[:8]
        on_site = normalized(
            raw_job(external_id=f"c-{suffix}", url=f"https://careers.acme.vn/jobs/c-{suffix}"),
            board=False,
        )
        on_board = normalized(
            raw_job(
                source="topcv",
                external_id=f"t-{suffix}",
                url=f"https://www.topcv.vn/viec-lam/ke-toan/{suffix}.html",
                salary_raw=None,
                expires_at=None,
                level="Senior",
            ),
            board=True,
        )
        repost = normalized(
            raw_job(external_id=f"c2-{suffix}", url=f"https://careers.acme.vn/jobs/c2-{suffix}"),
            board=False,
        )
        assert on_site.salary is not None

        async def save(
            jobs: list[NormalizedJob],
            *,
            complete: bool,
            absent: set[tuple[str, str]] | None = None,
        ) -> store.SaveResult:
            verdicts = [Verdict(accepted=True, confidence=0.9, reasons=["TEST"]) for _ in jobs]
            async with res.sessionmaker.begin() as s:
                return await store.save_company_jobs(
                    s,
                    company_id=company_id,
                    company_name="ACME",
                    jobs=jobs,
                    verdicts=verdicts,
                    clusters=dedup.cluster_jobs(jobs),
                    now=NOW,
                    complete=complete,
                    is_board={"topcv"}.__contains__,
                    absent=absent or set(),
                )

        [full] = (await save([on_site, on_board], complete=True)).jobs
        assert full.canonical_source == "careers"

        # Lượt poll `board`: chỉ thấy tin TopCV.
        [job] = (await save([on_board], complete=False)).jobs
        assert job.canonical_source == "careers"
        assert job.source_urls == [on_site.url, on_board.url]
        assert job.salary is not None
        assert job.salary.min == on_site.salary.min
        assert job.expires_at == on_site.expires_at
        assert job.level is JobLevel.SENIOR  # rule của tin TopCV
        async with res.sessionmaker() as s:
            rows = (await s.scalars(select(Job).where(Job.company_id == company_id))).all()
            canonical = await s.scalar(
                select(JobPosting.id).where(JobPosting.external_id == on_site.external_id)
            )
        assert [r.canonical_posting_id for r in rows] == [canonical]
        assert "DEDUP: gộp 2 tin" in rows[0].verification_reasons

        # Tin careers đã gỡ khỏi trang (lượt poll đọc trọn nguồn): không gộp lại.
        gone = {(on_site.source, on_site.external_id)}
        [alone] = (await save([on_board], complete=False, absent=gone)).jobs
        assert (alone.canonical_source, alone.source_urls) == ("topcv", [on_board.url])

        # Đăng lại với mã mới (lượt poll thường, tin cũ đã gỡ): gắn vào việc cũ, gộp với tin TopCV.
        [again] = (await save([repost], complete=False, absent=gone)).jobs
        assert again.canonical_source == "careers"
        assert again.source_urls == [repost.url, on_board.url]
        async with res.sessionmaker() as s:
            count = await s.scalar(
                select(func.count()).select_from(Job).where(Job.company_id == company_id)
            )
        assert count == 1


async def test_build_retires_careers_plan_on_a_job_board(integration_settings: Settings) -> None:
    """Bộ khung careers ghi từ trước trên chính sàn tuyển dụng (jobsgo.vn): lượt build bỏ nó dù
    lượt tìm nguồn chưa trọn (`retire_missing=False`) và gỡ tin nó đã ghi (tin của doanh nghiệp
    khác); bộ khung của trang tuyển dụng thật vẫn giữ."""
    from jobcrawler.modules.crawl import plan_service
    from jobcrawler.sources.plan import PlanSpec

    async with open_resources(integration_settings, db_logs=False) as res:
        async with res.sessionmaker.begin() as s:
            company = Company(input_name=f"ACME board plan {uuid4().hex[:6]}")
            s.add(company)
            await s.flush()
            company_id = company.id
        suffix = uuid4().hex[:8]
        other = normalized(
            raw_job(
                external_id=f"b-{suffix}",
                url=f"https://jobsgo.vn/viec-lam/x-{suffix}.html",
                title="Nhân viên kinh doanh",
                company_name="Công ty TNHH Khác",
            ),
            board=False,
        )
        own = normalized(
            raw_job(external_id=f"c-{suffix}", url=f"https://acme.vn/tuyen-dung/c-{suffix}"),
            board=False,
        )
        jobs = [other, own]
        async with res.sessionmaker.begin() as s:
            await store.save_company_jobs(
                s,
                company_id=company_id,
                company_name="ACME",
                jobs=jobs,
                verdicts=[Verdict(accepted=True, confidence=0.9, reasons=["TEST"]) for _ in jobs],
                clusters=dedup.cluster_jobs(jobs),
                now=NOW,
                complete=True,
                is_board=frozenset().__contains__,
                absent=set(),
            )
            s.add(
                CrawlPlan(
                    company_id=company_id,
                    source="careers:jobsgo.vn",
                    kind="careers",
                    url="https://jobsgo.vn/viec-lam.html",
                    spec={},
                    job_keys=[other.external_id],
                    status=PlanStatus.ACTIVE.value,
                    built_at=NOW,
                )
            )
            s.add(
                CrawlPlan(
                    company_id=company_id,
                    source="careers:vieclam.bao.vn",
                    kind="careers",
                    url="https://vieclam.bao.vn/",
                    spec={},
                    job_keys=[],
                    status=PlanStatus.ACTIVE.value,
                    built_at=NOW,
                )
            )
        legit = plan_service.BuiltPlan(
            source="careers:acme.vn",
            kind="careers",
            url="https://acme.vn/tuyen-dung",
            spec=PlanSpec(listings=["https://acme.vn/tuyen-dung"]),
            keys=[own.external_id],
            ok=True,
        )
        async with res.sessionmaker.begin() as s:
            usable = await plan_service.save_built_plans(
                s,
                company_id,
                [legit],
                run_id=uuid4(),
                now=NOW,
                retire_missing=False,
                job_boards=["careers:vieclam.bao.vn"],  # lượt build thấy là trang sàn
            )
        assert usable == 1
        async with res.sessionmaker() as s:
            statuses: dict[str, str] = dict(
                (
                    await s.execute(
                        select(CrawlPlan.source, CrawlPlan.status).where(
                            CrawlPlan.company_id == company_id
                        )
                    )
                ).all()
            )
            titles: dict[str, str] = dict(
                (
                    await s.execute(
                        select(JobPosting.external_id, Job.status)
                        .join(Job, Job.id == JobPosting.job_id)
                        .where(JobPosting.company_id == company_id)
                    )
                ).all()
            )
        assert statuses == {
            "careers:jobsgo.vn": PlanStatus.RETIRED.value,
            "careers:vieclam.bao.vn": PlanStatus.RETIRED.value,
            "careers:acme.vn": PlanStatus.ACTIVE.value,
        }
        assert titles[own.external_id] == JobStatus.ACTIVE.value
        assert titles[other.external_id] != JobStatus.ACTIVE.value
