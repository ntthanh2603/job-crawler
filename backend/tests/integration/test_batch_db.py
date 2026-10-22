"""`import` / `enqueue` trên Postgres + Redis thật."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from jobcrawler.common.config import Settings
from jobcrawler.common.schemas import CompanyIn, RunKind, RunStatus, RunTrigger
from jobcrawler.database.ids import uuid7
from jobcrawler.database.models import Company, CompanyAlias, CompanyRun
from jobcrawler.modules.companies import batch_service
from jobcrawler.modules.crawl import crawl_service
from jobcrawler.services.redis.queues import drop_queue
from jobcrawler.services.redis.streamqueue import StreamWorker
from jobcrawler.services.resources import Resources, open_resources

pytestmark = pytest.mark.integration


@pytest.fixture
async def res(integration_settings: Settings) -> AsyncIterator[Resources]:
    """Tài nguyên với ba hàng đợi rỗng tên ngẫu nhiên."""
    tag = uuid4().hex[:8]
    settings = integration_settings.model_copy(
        update={
            "build_queue_name": f"jobcrawler-test-build-{tag}",
            "poll_queue_name": f"jobcrawler-test-poll-{tag}",
            "board_queue_name": f"jobcrawler-test-boards-{tag}",
            "company_timeout_seconds": 180,
            "company_job_retries": 2,
        }
    )
    async with open_resources(settings, db_logs=False) as res:
        try:
            yield res
        finally:
            for name in settings.queue_names:
                await drop_queue(settings.redis_url, name)


async def test_import_is_idempotent(integration_settings: Settings) -> None:
    tag = uuid4().hex[:6]
    beta_tax = "0106" + str(int(tag, 16) % 10**6).zfill(6)
    rows = [
        CompanyIn(name=f"Công ty TNHH Alpha {tag}", website="alpha.vn"),
        CompanyIn(name=f"Công ty CP Beta {tag}", tax_code=beta_tax),
        CompanyIn(name=f"Gamma {tag}", note="ghi chú"),
    ]
    async with open_resources(integration_settings, db_logs=False) as res:
        first = await batch_service.import_companies(res.sessionmaker, rows)
        assert (first.created, first.updated, first.unchanged) == (3, 0, 0)
        assert len(first.company_ids) == 3

        again = await batch_service.import_companies(res.sessionmaker, rows)
        assert (again.created, again.updated, again.unchanged) == (0, 0, 3)
        assert again.company_ids == first.company_ids

        # Viết khác (hậu tố pháp lý), thêm website/MST: cập nhật đúng bản ghi cũ.
        changed = [
            CompanyIn(name=f"Alpha {tag} Co., Ltd", website="https://alpha.vn/tuyen-dung"),
            CompanyIn(name=f"Beta {tag}", tax_code=beta_tax),
            CompanyIn(name=f"Gamma {tag}", tax_code="0109999999"),
        ]
        third = await batch_service.import_companies(res.sessionmaker, changed)
        assert third.company_ids == first.company_ids
        assert (third.created, third.updated, third.unchanged) == (0, 2, 1)

        # Cùng tên nhưng khác MST: pháp nhân khác → bản ghi mới.
        other = await batch_service.import_companies(
            res.sessionmaker, [CompanyIn(name=f"Công ty TNHH Gamma {tag}", tax_code="0108888888")]
        )
        assert other.created == 1
        assert other.company_ids[0] not in first.company_ids

        async with res.sessionmaker() as s:
            alpha = await s.get(Company, first.company_ids[0])
            gamma = await s.get(Company, first.company_ids[2])
            aliases = (
                await s.scalars(
                    select(CompanyAlias.alias).where(CompanyAlias.company_id == alpha.id)  # pyright: ignore[reportOptionalMemberAccess]
                )
            ).all()
        assert alpha is not None
        assert alpha.input_website == "https://alpha.vn/tuyen-dung"
        assert alpha.kyc_status is None  # import không chạy KYC
        # "Alpha Co., Ltd" cùng tên chuẩn hoá với alias đã có: không thêm dòng.
        assert set(aliases) == {f"Công ty TNHH Alpha {tag}"}
        assert gamma is not None
        assert (gamma.tax_code, gamma.note) == ("0109999999", "ghi chú")


async def test_select_stale_companies(integration_settings: Settings) -> None:
    tag = uuid4().hex[:6]
    now = datetime.now(UTC)
    async with open_resources(integration_settings, db_logs=False) as res:
        imported = await batch_service.import_companies(
            res.sessionmaker, [CompanyIn(name=f"Stale {tag} {c}") for c in "ABCD"]
        )
        a, b, c, d = imported.company_ids
        async with res.sessionmaker.begin() as s:
            for company_id, kind, status, hours_ago in [
                (a, RunKind.BUILD, RunStatus.OK, 30),
                (a, RunKind.BUILD, RunStatus.FAILED, 1),  # lượt lỗi không tính là đã build
                (b, RunKind.BUILD, RunStatus.PARTIAL, 2),
                (c, RunKind.BUILD, RunStatus.FAILED, 1),
                (d, RunKind.POLL, RunStatus.OK, 1),  # lượt poll không tính là đã build
            ]:
                started = now - timedelta(hours=hours_ago)
                s.add(
                    CompanyRun(
                        run_id=uuid4(),
                        company_id=company_id,
                        kind=kind.value,
                        started_at=started,
                        finished_at=started + timedelta(minutes=2),
                        status=status.value,
                    )
                )
        async with res.sessionmaker() as s:
            ids = imported.company_ids
            stale = await batch_service.select_companies(
                s, company_ids=ids, stale_hours=24, now=now
            )
            assert [x.id for x in stale] == [a, c, d]
            # Hộp thoại Build đếm trước bằng cùng điều kiện.
            for hours in (24, None):
                every = await batch_service.select_companies(s, stale_hours=hours, now=now)
                count = await batch_service.count_companies(s, stale_hours=hours, now=now)
                assert count == len(every)
            assert [
                x.id for x in await batch_service.select_companies(s, company_ids=[d, b, a])
            ] == [
                d,
                b,
                a,
            ]
            limited = await batch_service.select_companies(
                s, company_ids=ids, stale_hours=1, limit=2
            )
            assert [x.id for x in limited] == [a, b]


async def test_enqueue_dedupes_by_company(res: Resources) -> None:
    tag = uuid4().hex[:6]
    imported = await batch_service.import_companies(
        res.sessionmaker,
        [
            CompanyIn(name=f"Queue {tag} A", website="a.vn"),
            CompanyIn(name=f"Queue {tag} B", tax_code="0107777777"),
        ],
    )
    async with res.sessionmaker() as s:
        companies = await batch_service.select_companies(s, company_ids=imported.company_ids)
    first = await batch_service.enqueue_companies(res, companies)
    assert (first.selected, first.enqueued, first.already_queued) == (2, 2, 0)
    second = await batch_service.enqueue_companies(res, companies)
    assert (second.enqueued, second.already_queued) == (0, 2)
    info = await res.queues.build.info()
    assert info["queued"] == 2
    job = await res.queues.build.job(crawl_service.build_key(companies[0].id))
    assert job is not None
    assert job.function == "build_company"
    assert (job.timeout, job.retries) == (180, 2)
    assert job.kwargs == {"company_id": str(companies[0].id), "trigger": RunTrigger.MANUAL.value}


async def test_enqueue_ten_thousand(res: Resources) -> None:
    """10.000 doanh nghiệp vào queue trong vài giây (không cần ghi DB)."""
    companies = [Company(id=uuid7(), input_name=f"Doanh nghiệp số {i}") for i in range(10_000)]
    started = time.monotonic()
    result = await batch_service.enqueue_companies(res, companies)
    elapsed = time.monotonic() - started
    assert result.enqueued == 10_000
    assert (await res.queues.build.info())["queued"] == 10_000
    assert elapsed < 60, elapsed


async def test_timed_out_job_is_retried_once(res: Resources) -> None:
    """Job quá timeout cứng bị huỷ và chạy lại tối đa COMPANY_JOB_RETRIES lần (ở đây 2 lần)."""
    attempts: list[int] = []
    queue = res.queues.build

    async def slow(ctx: dict[str, object], **_: object) -> None:
        attempts.append(ctx["job"].attempts)  # pyright: ignore[reportAttributeAccessIssue]
        await asyncio.sleep(5)

    worker = StreamWorker(
        queue, functions=[("build_company", slow)], concurrency=1, handle_signals=False
    )
    job = await queue.enqueue("build_company", key="build:slow", timeout=1, retries=2)
    assert job is not None
    running = asyncio.create_task(worker.start())
    try:
        async with asyncio.timeout(30):
            while (done := await queue.job("build:slow")) is None or done.status != "failed":  # noqa: ASYNC110
                await asyncio.sleep(0.2)
    finally:
        worker.stop()
        await running
    assert attempts == [1, 2]
    assert done is not None
    assert done.status == "failed"


async def test_company_summary_tracks_the_latest_build(integration_settings: Settings) -> None:
    """`companies.last_run_*` (cột "Build gần nhất" của danh sách) là lượt build mới nhất: bảo trì
    đối chiếu lại không lấy lượt poll mới hơn."""
    from jobcrawler.database.models import Company
    from jobcrawler.modules.maintenance import maintenance_service

    tag = uuid4().hex[:6]
    now = datetime.now(UTC)
    async with open_resources(integration_settings, db_logs=False) as res:
        [company_id] = (
            await batch_service.import_companies(
                res.sessionmaker, [CompanyIn(name=f"Summary {tag}")]
            )
        ).company_ids
        build_id = uuid4()
        async with res.sessionmaker.begin() as s:
            runs = [(build_id, RunKind.BUILD, 5), (uuid4(), RunKind.POLL, 1)]
            for run_id, kind, hours_ago in runs:
                started = now - timedelta(hours=hours_ago)
                s.add(
                    CompanyRun(
                        run_id=run_id,
                        company_id=company_id,
                        kind=kind.value,
                        started_at=started,
                        finished_at=started + timedelta(minutes=1),
                        status=RunStatus.OK.value,
                    )
                )
        async with res.sessionmaker.begin() as s:
            await maintenance_service.refresh_company_summaries(s)
        async with res.sessionmaker() as s:
            company = await s.get(Company, company_id)
        assert company is not None
        assert company.last_run_id == build_id
