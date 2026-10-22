"""Outbox sự kiện việc làm trên Postgres thật: phát đúng loại ở mọi đường đổi `jobs`, gán seq theo
thứ tự đã commit, feed / snapshot theo phạm vi client, dọn đầu feed (modules/integration)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, insert, select

from jobcrawler.common.config import Settings
from jobcrawler.common.schemas import JobEventType, JobStatus
from jobcrawler.database.models import ApiClient, Company, CompanyRef, Job, JobEvent
from jobcrawler.modules.integration import feed_service, relay_service
from jobcrawler.modules.maintenance import maintenance_service
from jobcrawler.pipeline import dedup, kyc, store
from jobcrawler.pipeline.verify import Verdict
from jobcrawler.services.resources import Resources, open_resources

from ..jobs_fixtures import NOW, normalized, raw_job

pytestmark = pytest.mark.integration


async def new_company(res: Resources, name: str = "ACME") -> UUID:
    async with res.sessionmaker.begin() as s:
        company = Company(input_name=f"{name} {uuid4().hex[:8]}")
        s.add(company)
        await s.flush()
        return company.id


async def new_client(res: Resources, *companies: UUID, all_companies: bool = False) -> UUID:
    async with res.sessionmaker.begin() as s:
        client = ApiClient(name=f"test-{uuid4().hex[:8]}", all_companies=all_companies)
        s.add(client)
        await s.flush()
        for i, company_id in enumerate(companies):
            s.add(CompanyRef(client_id=client.id, external_id=f"ext-{i}", company_id=company_id))
        return client.id


def posting(code: str, suffix: str, **fields: Any) -> Any:
    return normalized(
        raw_job(
            external_id=f"{code}-{suffix}",
            url=f"https://careers.acme.vn/jobs/{code}-{suffix}",
            **fields,
        ),
        board=False,
    )


async def save(res: Resources, company_id: UUID, jobs: list, *, now: datetime) -> None:
    verdicts = [Verdict(accepted=True, confidence=0.9, reasons=["TEST"]) for _ in jobs]
    async with res.sessionmaker.begin() as s:
        await store.save_company_jobs(
            s,
            company_id=company_id,
            company_name="ACME",
            jobs=jobs,
            verdicts=verdicts,
            clusters=dedup.cluster_jobs(jobs),
            now=now,
            complete=True,
        )


async def events_of(res: Resources, company_id: UUID) -> list[tuple[UUID, str]]:
    async with res.sessionmaker() as s:
        rows = await s.execute(
            select(JobEvent.job_id, JobEvent.type)
            .where(JobEvent.company_id == company_id)
            .order_by(JobEvent.id)
        )
        return [(r.job_id, r.type) for r in rows]


async def sequence_all(res: Resources) -> int:
    while True:
        async with res.sessionmaker.begin() as s:
            assigned, last = await feed_service.sequence(s)
        if not assigned:
            return last


async def test_job_lifecycle_emits_one_event_per_change(integration_settings: Settings) -> None:
    async with open_resources(integration_settings, db_logs=False) as res:
        company_id = await new_company(res)
        suffix = uuid4().hex[:8]

        await save(res, company_id, [posting("a", suffix)], now=NOW)
        [(job_id, kind)] = await events_of(res, company_id)
        assert kind == "created"

        # Ghi lại y hệt (lượt build đêm), kể cả ngày đăng tính lại: không có sự kiện.
        later = NOW + timedelta(hours=8)
        repost = posting("a", suffix, posted_at=datetime(2026, 10, 11, tzinfo=UTC))
        await save(res, company_id, [repost], now=later)
        assert len(await events_of(res, company_id)) == 1

        changed = posting("a", suffix, salary_raw="25 - 30 triệu")
        await save(res, company_id, [changed], now=later + timedelta(hours=1))
        assert [k for _, k in await events_of(res, company_id)] == ["created", "updated"]

        await save(res, company_id, [], now=later + timedelta(hours=2))
        await save(res, company_id, [posting("b", suffix)], now=later + timedelta(days=1))
        events = await events_of(res, company_id)
        assert [k for _, k in events] == ["created", "updated", "closed", "reopened"]
        assert {j for j, _ in events} == {job_id}


async def test_hash_baseline_after_upgrade_emits_nothing(integration_settings: Settings) -> None:
    """Job lưu trước migration 0007 (hash NULL): lượt ghi lại đầu tiên chỉ ghi mốc."""
    async with open_resources(integration_settings, db_logs=False) as res:
        company_id = await new_company(res)
        suffix = uuid4().hex[:8]
        await save(res, company_id, [posting("a", suffix)], now=NOW)
        async with res.sessionmaker.begin() as s:
            job = await s.scalar(select(Job).where(Job.company_id == company_id))
            assert job is not None
            assert job.content_hash
            job.content_hash = None
        changed = posting("a", suffix, salary_raw="25 - 30 triệu")
        await save(res, company_id, [changed], now=NOW + timedelta(hours=1))
        assert [k for _, k in await events_of(res, company_id)] == ["created"]


async def test_close_postings_and_maintenance_emit(integration_settings: Settings) -> None:
    async with open_resources(integration_settings, db_logs=False) as res:
        company_id = await new_company(res)
        suffix = uuid4().hex[:8]
        jobs = [posting("a", suffix), posting("b", suffix, title="Lập trình viên Java")]
        await save(res, company_id, jobs, now=NOW)
        async with res.sessionmaker.begin() as s:
            closed = await store.close_postings(
                s,
                company_id,
                {("careers", f"a-{suffix}")},
                present=set(),
                boards=set(),
                now=NOW + timedelta(hours=1),
            )
        assert closed == 1
        events = await events_of(res, company_id)
        assert [k for _, k in events] == ["created", "created", "closed"]
        async with res.sessionmaker() as s:
            open_job = await s.scalar(
                select(Job.id).where(
                    Job.company_id == company_id, Job.status == JobStatus.ACTIVE.value
                )
            )
        assert open_job is not None

        # Làm sạch lại nội dung: `updated`, hash về NULL để lượt sau không phát lần nữa.
        async with res.sessionmaker.begin() as s:
            await maintenance_service._rewrite(  # pyright: ignore[reportPrivateUsage]
                s, open_job, company_id, {"description_md": "Mô tả đã làm sạch lại."}
            )
            assert await s.scalar(select(Job.content_hash).where(Job.id == open_job)) is None
        assert (await events_of(res, company_id))[-1] == (open_job, "updated")


async def test_merge_moves_refs_and_reports_moved_and_dropped_jobs(
    integration_settings: Settings,
) -> None:
    async with open_resources(integration_settings, db_logs=False) as res:
        src_id = await new_company(res, "SRC")
        dst_id = await new_company(res, "DST")
        suffix = uuid4().hex[:8]
        same = posting("same", suffix)
        only = posting("only", suffix, title="Lập trình viên Java")
        await save(res, src_id, [same, only], now=NOW)
        await save(res, dst_id, [posting("same", f"{suffix}x")], now=NOW)
        client_id = await new_client(res, src_id)
        async with res.sessionmaker() as s:
            src_jobs = {
                r.title_normalized: r.id
                for r in await s.scalars(select(Job).where(Job.company_id == src_id))
            }
        async with res.sessionmaker.begin() as s:
            src = await s.get(Company, src_id)
            dst = await s.get(Company, dst_id)
            assert src is not None
            assert dst is not None
            await kyc.merge_companies(s, src, dst)
        async with res.sessionmaker() as s:
            ref = await s.scalar(select(CompanyRef).where(CompanyRef.client_id == client_id))
        assert ref is not None
        assert ref.company_id == dst_id
        moved = [(j, k) for j, k in await events_of(res, dst_id) if j in src_jobs.values()]
        # Cùng tiêu đề nên cùng dấu vân tay với job của dst: bị xoá; job còn lại chuyển sang.
        assert sorted(k for _, k in moved) == ["deleted", "updated"]


async def test_sequence_follows_commit_order(integration_settings: Settings) -> None:
    """Transaction ghi sự kiện trước (id nhỏ hơn) nhưng commit sau: vẫn nhận seq sau seq đã cấp,
    người đọc cursor cũ không bỏ sót."""
    async with open_resources(integration_settings, db_logs=False) as res:
        company_id = await new_company(res)
        client_id = await new_client(res, company_id)
        start = await sequence_all(res)
        scope = feed_service.Scope(client_id)

        early = res.sessionmaker()
        late_job, early_job = uuid4(), uuid4()
        try:
            await early.begin()
            await store.emit_job_events(early, [(early_job, company_id, JobEventType.CREATED)])
            await early.flush()
            async with res.sessionmaker.begin() as s:
                await store.emit_job_events(s, [(late_job, company_id, JobEventType.CREATED)])
            await sequence_all(res)
            async with res.sessionmaker() as s:
                first = await feed_service.changes(s, scope, start, limit=100, now=NOW)
            assert [e.job_id for e in first.events] == [late_job]
            assert first.events[0].job is None  # không có dòng jobs: chỉ có sự kiện
            await early.commit()
        finally:
            await early.close()
        await sequence_all(res)
        async with res.sessionmaker() as s:
            second = await feed_service.changes(
                s, scope, int(first.next_cursor), limit=100, now=NOW
            )
        assert [e.job_id for e in second.events] == [early_job]
        assert second.events[0].seq > first.events[0].seq
        assert not second.has_more


async def test_feed_scope_snapshot_and_limits(integration_settings: Settings) -> None:
    async with open_resources(integration_settings, db_logs=False) as res:
        mine = await new_company(res, "MINE")
        other = await new_company(res, "OTHER")
        client_id = await new_client(res, mine)
        async with res.sessionmaker.begin() as s:
            s.add(CompanyRef(client_id=client_id, external_id="alias-of-mine", company_id=mine))
        start = await sequence_all(res)
        suffix = uuid4().hex[:8]
        titles = ["Kế toán tổng hợp", "Lập trình viên Java", "Nhân viên kinh doanh"]
        await save(
            res, mine, [posting(f"m{i}", suffix, title=t) for i, t in enumerate(titles)], now=NOW
        )
        await save(res, other, [posting("o", suffix)], now=NOW)
        head = await sequence_all(res)

        scope = feed_service.Scope(client_id, include_description=False)
        async with res.sessionmaker() as s:
            page = await feed_service.changes(s, scope, start, limit=2, now=NOW)
            assert [e.type for e in page.events] == [JobEventType.CREATED] * 2
            assert page.has_more
            assert int(page.next_cursor) == page.events[-1].seq
            job = page.events[0].job
            assert job is not None
            assert job.company_external_ids == ["alias-of-mine", "ext-0"]
            assert job.description_md == ""
            assert job.requirements_md is None
            rest = await feed_service.changes(s, scope, int(page.next_cursor), limit=10, now=NOW)
            assert len(rest.events) == 1
            assert rest.next_cursor == str(head)
            assert not rest.has_more

            # Quét tối đa `scan` seq: client không có sự kiện trong đoạn vẫn tiến cursor.
            empty = await feed_service.changes(s, scope, start, limit=10, now=NOW, scan=0)
            assert empty.events == []
            assert empty.next_cursor == str(start)

            everything = feed_service.Scope(client_id, all_companies=True)
            all_events = await feed_service.changes(s, everything, start, limit=100, now=NOW)
            assert {e.company_id for e in all_events.events} >= {mine, other}

            snap = await feed_service.snapshot(s, scope, limit=2, now=NOW)
            assert snap.feed_cursor == str(head)
            assert snap.next_cursor is not None
            snap2 = await feed_service.snapshot(
                s, scope, after=UUID(snap.next_cursor), limit=2, now=NOW
            )
            assert snap2.feed_cursor is None
            assert snap2.next_cursor is None
            assert len(snap.data) + len(snap2.data) == 3
            assert all(j.company_id == mine for j in snap.data + snap2.data)

            with pytest.raises(feed_service.ResyncRequired):
                await feed_service.changes(s, scope, head + 1, limit=10, now=NOW)


async def test_prune_keeps_unsequenced_events_and_marks_resync(
    integration_settings: Settings,
) -> None:
    async with open_resources(integration_settings, db_logs=False) as res:
        company_id = await new_company(res)
        client_id = await new_client(res, company_id)
        old = await sequence_all(res)
        async with res.sessionmaker.begin() as s:
            await store.emit_job_events(s, [(uuid4(), company_id, JobEventType.CREATED)])
        head = await sequence_all(res)
        async with res.sessionmaker.begin() as s:
            pending = uuid4()
            await s.execute(
                insert(JobEvent).values(job_id=pending, company_id=company_id, type="created")
            )
        future = datetime.now(UTC) + timedelta(days=1)
        deleted = await maintenance_service.prune_job_events(res.sessionmaker, future)
        assert deleted >= 1
        async with res.sessionmaker() as s:
            pruned, last = await feed_service.bounds(s)
            assert pruned == head == last
            assert await s.scalar(select(JobEvent.id).where(JobEvent.job_id == pending))
            with pytest.raises(feed_service.ResyncRequired):
                await feed_service.changes(s, feed_service.Scope(client_id), old, limit=10, now=NOW)
            ok = await feed_service.changes(
                s, feed_service.Scope(client_id), head, limit=10, now=NOW
            )
            assert ok.events == []


async def test_relay_sequences_with_one_leader_and_publishes_head(
    integration_settings: Settings,
) -> None:
    settings = integration_settings.model_copy(
        update={"redis_prefix": f"jc-relay-{uuid4().hex[:6]}", "relay_interval_seconds": 0.2}
    )
    async with open_resources(settings, db_logs=False) as res:
        company_id = await new_company(res)
        async with res.sessionmaker.begin() as s:
            await store.emit_job_events(s, [(uuid4(), company_id, JobEventType.CREATED)])
        stop = asyncio.Event()
        relays = [asyncio.create_task(relay_service.run_relay(res, stop)) for _ in range(2)]
        key = feed_service.head_key(settings.redis_prefix)
        try:
            for _ in range(100):
                published = await res.redis.get(key)
                async with res.sessionmaker() as s:
                    pending = await s.scalar(
                        select(func.count()).select_from(JobEvent).where(JobEvent.seq.is_(None))
                    )
                if published is not None and not pending:
                    break
                await asyncio.sleep(0.1)
        finally:
            stop.set()
            await asyncio.gather(*relays)
        async with res.sessionmaker() as s:
            _, last = await feed_service.bounds(s)
        assert published is not None
        assert int(published) == last
        assert await res.redis.get(relay_service.lease_key(settings.redis_prefix)) is None
