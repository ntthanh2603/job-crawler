"""M6 trên Postgres + Redis thật, không gọi mạng: enqueue → runner build (Redis Streams,
concurrency 10) → deadline/partial/timeout cứng → stats → eval → chạy lại idempotent → bảo trì."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text

from jobcrawler.bootstrap import runner as runner_mod
from jobcrawler.common.config import Settings
from jobcrawler.common.schemas import CompanyIn, FetchOutcome, RunStatus
from jobcrawler.database.models import CompanyRun, FailedPage, Job, Log, SourceFetch
from jobcrawler.modules.companies import batch_service
from jobcrawler.modules.crawl import company_service
from jobcrawler.modules.evaluate import evaluate_service
from jobcrawler.modules.maintenance import maintenance_service
from jobcrawler.modules.overview import stats_service
from jobcrawler.services.fakes import FakeFetcher
from jobcrawler.services.net.http import FetchResult
from jobcrawler.services.redis.queues import drop_queue, make_queue
from jobcrawler.services.redis.streamqueue import StreamWorker
from jobcrawler.services.resources import Resources, open_resources
from jobcrawler.sources import boards

pytestmark = pytest.mark.integration

N = 30
SLOW = {3, 7, 11, 19, 23, 29}  # API ATS treo quá deadline → partial
DEADLINE = 2.0
TIMEOUT = 4


def lever_api(site: str) -> str:
    return f"https://api.lever.co/v0/postings/{site}?mode=json"


JD = (
    "<p>Tham gia phát triển và vận hành hệ thống bán hàng cho hàng nghìn cửa hàng trên toàn"
    " quốc. Phối hợp với đội sản phẩm để phân tích yêu cầu, thiết kế giải pháp, viết tài liệu"
    " kỹ thuật và hỗ trợ khách hàng khi cần. Báo cáo tiến độ hằng tuần cho trưởng nhóm.</p>"
)


def postings(site: str, i: int) -> list[dict[str, Any]]:
    created = int((datetime.now(UTC) - timedelta(days=3)).timestamp() * 1000)
    return [
        {
            "id": f"{site}-{k}",
            "text": title,
            "categories": {"location": "Hà Nội, Việt Nam", "commitment": "Full-time"},
            "description": JD,
            "lists": [{"text": "Yêu cầu", "content": "<li>Tốt nghiệp đại học</li>"}],
            "hostedUrl": f"https://jobs.lever.co/{site}/{site}-{k}",
            "createdAt": created,
        }
        for k, title in enumerate([f"Kỹ sư phần mềm nhóm {i}", f"Chuyên viên kế toán nhóm {i}"])
    ]


class ScaleFetcher(FakeFetcher):
    slow: set[str] = set()  # noqa: RUF012

    async def fetch(self, url: str, **kwargs: Any) -> FetchResult:
        if any(site in url for site in self.slow):
            await asyncio.sleep(30)
        return await super().fetch(url, **kwargs)


@dataclasses.dataclass
class Scale:
    res: Resources
    settings: Settings
    tag: str
    sites: list[str]
    company_ids: list[UUID]
    hang_id: UUID


@pytest.fixture
async def scale(
    integration_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Scale]:
    tag = uuid4().hex[:6]
    settings = integration_settings.model_copy(
        update={
            "build_queue_name": f"jobcrawler-test-build-{tag}",
            "poll_queue_name": f"jobcrawler-test-poll-{tag}",
            "board_queue_name": f"jobcrawler-test-boards-{tag}",
            "redis_prefix": f"jc-test-{tag}",
            "llm_base_url": None,
            "serp_api_key": None,
            "board_serp_lookup": False,
            "company_deadline_seconds": DEADLINE,
            "company_timeout_seconds": TIMEOUT,
            "kyc_timeout_seconds": 1.0,
            "discovery_timeout_seconds": 1.0,
            "runner_build_concurrency": 10,
            "company_job_retries": 2,
        }
    )
    monkeypatch.setattr(boards, "registry", dict)  # chỉ nguồn ATS giả
    sites = [f"scale{tag}n{i}" for i in range(N)]
    fetcher = ScaleFetcher()
    fetcher.slow = {sites[i] for i in SLOW}
    for i, site in enumerate(sites):
        fetcher.add(lever_api(site), json.dumps(postings(site, i)), content_type="application/json")

    # Doanh nghiệp "treo": bước sau thu thập kẹt quá timeout cứng của job.
    original = company_service.process_jobs

    async def process_jobs(res: Resources, company: Any, raws: Any, **kwargs: Any) -> Any:
        if company.input_name.endswith("treo"):
            await asyncio.sleep(30)
        return await original(res, company, raws, **kwargs)

    monkeypatch.setattr(company_service, "process_jobs", process_jobs)
    async with open_resources(settings, db_logs=False) as res:
        offline = dataclasses.replace(res, http=fetcher)  # pyright: ignore[reportArgumentType]
        rows = [
            CompanyIn(name=f"Scale {tag} số {i}", website=f"https://jobs.lever.co/{site}")
            for i, site in enumerate(sites)
        ]
        rows.append(
            CompanyIn(name=f"Scale {tag} treo", website=f"https://jobs.lever.co/{sites[0]}")
        )
        imported = await batch_service.import_companies(res.sessionmaker, rows)
        try:
            yield Scale(
                res=offline,
                settings=settings,
                tag=tag,
                sites=sites,
                company_ids=imported.company_ids[:N],
                hang_id=imported.company_ids[N],
            )
        finally:
            for name in settings.queue_names:
                await drop_queue(settings.redis_url, name)


async def finished_runs(sc: Scale) -> int:
    async with sc.res.sessionmaker() as s:
        return int(
            await s.scalar(
                select(func.count()).where(
                    CompanyRun.company_id.in_([*sc.company_ids, sc.hang_id]),
                    CompanyRun.status != RunStatus.RUNNING.value,
                )
            )
            or 0
        )


async def run_worker_until(sc: Scale, expected_runs: int, *, limit: float = 90) -> float:
    """Chạy runner build thật (như `jobcrawler runner --role build`, không cron) tới khi đủ số
    lượt chạy đã xong."""
    queue = make_queue(sc.settings, sc.settings.build_queue_name)
    w = StreamWorker(
        queue,
        functions=[runner_mod.build_company],
        concurrency=sc.settings.runner_build_concurrency,
        cancellation_hard_deadline_s=runner_mod.CANCEL_GRACE_SECONDS,
        handle_signals=False,
    )
    w.context["settings"] = sc.settings
    w.context["resources"] = sc.res
    await queue.connect()
    started = time.monotonic()
    task = asyncio.create_task(w.start())
    try:
        while await finished_runs(sc) < expected_runs:
            assert time.monotonic() - started < limit, "worker chạy quá lâu"
            assert not task.done(), task
            await asyncio.sleep(0.5)
        # Chờ queue rỗng (job đã ghi xong kết quả vào hàng đợi).
        while (info := await queue.info())["queued"] or info["active"]:
            assert time.monotonic() - started < limit
            await asyncio.sleep(0.2)
        return time.monotonic() - started
    finally:
        w.stop()
        await asyncio.wait({task}, timeout=20)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await queue.disconnect()


async def enqueue_all(sc: Scale) -> batch_service.EnqueueResult:
    async with sc.res.sessionmaker() as s:
        selected = await batch_service.select_companies(
            s, company_ids=[*sc.company_ids, sc.hang_id]
        )
    return await batch_service.enqueue_companies(sc.res, selected)


async def test_worker_scale_deadline_stats_eval(scale: Scale, tmp_path: Path) -> None:
    sc = scale
    since = datetime.now(UTC)
    first = await enqueue_all(sc)
    assert first.enqueued == N + 1
    assert (await enqueue_all(sc)).already_queued == N + 1  # khoá job chống nạp trùng

    elapsed = await run_worker_until(sc, expected_runs=N + 2)  # doanh nghiệp treo chạy 2 lần
    # 31 doanh nghiệp, 10 song song, chậm nhất ~2 s (deadline) và 2 × 4 s (timeout cứng).
    assert elapsed < 30, elapsed

    async with sc.res.sessionmaker() as s:
        runs = list(
            await s.scalars(
                select(CompanyRun)
                .where(CompanyRun.company_id.in_([*sc.company_ids, sc.hang_id]))
                .order_by(CompanyRun.started_at)
            )
        )
    by_company: dict[UUID, list[CompanyRun]] = {}
    for run in runs:
        by_company.setdefault(run.company_id, []).append(run)
    for i, company_id in enumerate(sc.company_ids):
        [run] = by_company[company_id]
        assert run.duration_ms is not None
        assert run.duration_ms <= TIMEOUT * 1000
        if i in SLOW:
            assert run.status == RunStatus.PARTIAL.value
            assert run.incomplete_sources == [f"lever:{sc.sites[i]}"]
            assert run.duration_ms >= (DEADLINE - 0.1) * 1000  # đồng hồ Windows ~15 ms
            assert run.jobs_accepted == 0
        else:
            assert run.status == RunStatus.OK.value, run.error
            assert (run.jobs_found, run.jobs_accepted) == (2, 2)
    hang = by_company[sc.hang_id]
    assert [r.status for r in hang] == [RunStatus.FAILED.value] * 2  # chạy lại một lần
    assert all(r.error == "cancelled" for r in hang)
    assert all((r.duration_ms or 0) <= TIMEOUT * 1000 + 1000 for r in hang)

    # Job đã lưu, mô tả nén zstd trong DB, đọc lại qua ORM như chuỗi.
    fast_ids = [c for i, c in enumerate(sc.company_ids) if i not in SLOW]
    async with sc.res.sessionmaker() as s:
        jobs = list(await s.scalars(select(Job).where(Job.company_id.in_(fast_ids))))
        heads = set(
            await s.scalars(
                text("SELECT get_byte(description_md, 0) FROM jobs WHERE company_id = ANY(:ids)"),
                {"ids": fast_ids},
            )
        )
    assert len(jobs) == 2 * len(fast_ids)
    assert heads == {1}
    assert all("hệ thống bán hàng" in j.description_md for j in jobs)
    job_ids = sorted(j.id for j in jobs)

    # stats: p95 trong timeout, tỉ lệ failed theo lượt mới nhất.
    async with sc.res.sessionmaker() as s:
        st = await stats_service.run_stats(s, since, timeout_seconds=TIMEOUT)
        sources = await stats_service.source_stats(s, since)
    assert st.runs >= N + 2
    assert st.p95_seconds is not None
    assert st.p95_seconds <= TIMEOUT + 1
    # Lượt chạy từ queue ghi lúc job sẵn sàng: đo được thời gian chờ (10 slot, 31 doanh nghiệp).
    assert all(r.queued_at is not None and r.queued_at <= r.started_at for r in runs)
    assert st.wait_p95_seconds is not None
    assert 0 <= st.wait_p95_seconds < 30
    assert st.latest_by_status.get(RunStatus.PARTIAL.value, 0) >= len(SLOW)
    assert st.failed_rate is not None
    assert st.failed_rate > 0
    assert ("lever", len(SLOW)) in st.incomplete_sources
    lever = next(src for src in sources if src.kind == "lever")
    assert lever.outcomes[FetchOutcome.OK.value] >= N - len(SLOW)
    full = await stats_service.collect_stats(sc.res, hours=1, storage=False)
    assert [q.name for q in full.queues] == list(sc.settings.queue_names)
    assert full.queues[0].queued == 0

    # eval trên golden set dựng từ dữ liệu giả: thêm một job không có để recall < 100%.
    golden = tmp_path / "golden"
    golden.mkdir()
    names = [f"Scale {sc.tag} số {i}" for i in (0, 1, 3)]
    (golden / "companies.csv").write_text("name\n" + "\n".join(names) + "\n", encoding="utf-8")
    lines = ["company,title,url"]
    for i in (0, 1, 3):
        lines.append(f"{names[(0, 1, 3).index(i)]},Kỹ sư phần mềm nhóm {i},")
        lines.append(
            f"{names[(0, 1, 3).index(i)]},,https://jobs.lever.co/{sc.sites[i]}/{sc.sites[i]}-1"
        )
    lines.append(f"{names[0]},Trưởng phòng nhân sự,")
    (golden / "jobs.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    report = await evaluate_service.evaluate(sc.res, evaluate_service.load_golden(golden))
    by_name = {c.name: c for c in report.companies}
    assert (by_name[names[0]].matched, by_name[names[0]].golden) == (2, 3)
    assert by_name[names[0]].missing == ["Trưởng phòng nhân sự"]
    assert by_name[names[1]].precision == by_name[names[1]].recall == 1.0
    assert by_name[names[2]].run_status == RunStatus.PARTIAL.value  # nguồn chậm: chưa có job
    assert by_name[names[2]].recall == 0.0
    assert report.precision == 1.0
    assert report.recall == pytest.approx(4 / 7)
    [lever_eval] = report.sources
    assert (lever_eval.source, lever_eval.predicted, lever_eval.matched) == ("lever", 4, 4)
    async with sc.res.sessionmaker() as s:
        sample = await evaluate_service.sample_jobs(s, n=5, seed=1)
        same = await evaluate_service.sample_jobs(s, n=5, seed=1)
    assert len(sample) == 5
    assert [j.job_id for j in sample] == [j.job_id for j in same]  # seed cố định → lặp lại được
    assert all(j.title and j.url and j.excerpt for j in sample)
    evaluate_service.write_sample_csv(tmp_path / "sample.csv", sample)
    assert evaluate_service.read_review(tmp_path / "sample.csv").unknown == 5

    # Chạy lại cả lô: không sinh job trùng, giữ nguyên id.
    assert (await enqueue_all(sc)).enqueued == N + 1
    await run_worker_until(sc, expected_runs=2 * (N + 2))
    async with sc.res.sessionmaker() as s:
        again = sorted(await s.scalars(select(Job.id).where(Job.company_id.in_(fast_ids))))
    assert again == job_ids


async def test_maintenance_prunes_and_closes_abandoned_runs(
    integration_settings: Settings,
) -> None:
    tag = uuid4().hex[:6]
    now = datetime.now(UTC)
    async with open_resources(integration_settings, db_logs=False) as res:
        imported = await batch_service.import_companies(
            res.sessionmaker, [CompanyIn(name=f"Maint {tag}")]
        )
        [company_id] = imported.company_ids
        stuck, fresh = uuid4(), uuid4()
        async with res.sessionmaker.begin() as s:
            s.add_all(
                [
                    CompanyRun(
                        run_id=stuck,
                        company_id=company_id,
                        started_at=now - timedelta(hours=1),
                        status=RunStatus.RUNNING.value,
                    ),
                    CompanyRun(
                        run_id=fresh,
                        company_id=company_id,
                        started_at=now - timedelta(seconds=30),
                        status=RunStatus.RUNNING.value,
                    ),
                ]
            )
            await s.flush()
            s.add_all(
                [
                    SourceFetch(
                        source=f"maint:{tag}",
                        url="https://x.vn/old",
                        outcome="ok",
                        fetched_at=now - timedelta(days=40),
                    ),
                    SourceFetch(source=f"maint:{tag}", url="https://x.vn/new", outcome="ok"),
                    Log(level="info", event=f"maint.{tag}", ts=now - timedelta(days=40)),
                    Log(level="info", event=f"maint.{tag}"),
                    FailedPage(
                        url="https://x.vn/a",
                        source=f"maint:{tag}",
                        reason="no_job",
                        html="<html>cũ</html>",
                        created_at=now - timedelta(days=8),
                    ),
                    FailedPage(
                        url="https://x.vn/b",
                        source=f"maint:{tag}",
                        reason="no_job",
                        html="<html>mới " + "x" * 5000 + "</html>",
                    ),
                ]
            )

        report = await maintenance_service.run_maintenance(res, compression=False)
        assert report.skipped is None
        assert report.abandoned_runs >= 1
        assert report.deleted["source_fetches"] >= 1
        assert report.deleted["logs"] >= 1
        assert report.deleted["failed_pages"] >= 1

        async with res.sessionmaker() as s:
            statuses = {
                r.run_id: (r.status, r.error)
                for r in await s.scalars(
                    select(CompanyRun).where(CompanyRun.run_id.in_([stuck, fresh]))
                )
            }
            urls = set(
                await s.scalars(select(SourceFetch.url).where(SourceFetch.source == f"maint:{tag}"))
            )
            logs = await s.scalar(
                select(func.count()).select_from(Log).where(Log.event == f"maint.{tag}")
            )
            pages = list(
                await s.scalars(select(FailedPage).where(FailedPage.source == f"maint:{tag}"))
            )
        assert statuses[stuck][0] == RunStatus.FAILED.value
        assert "abandoned" in (statuses[stuck][1] or "")
        assert statuses[fresh][0] == RunStatus.RUNNING.value  # còn trong hạn: không đụng
        assert urls == {"https://x.vn/new"}
        assert logs == 1
        assert [p.url for p in pages] == ["https://x.vn/b"]
        assert pages[0].html is not None
        assert pages[0].html.startswith("<html>mới")
