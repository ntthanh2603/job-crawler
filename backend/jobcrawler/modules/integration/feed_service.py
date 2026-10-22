"""Feed sự kiện việc làm cho hệ thống ngoài (outbox `job_events`, docs/ARCHITECTURE.md mục 12).

Mọi chỗ đổi `jobs` ghi một dòng `job_events` trong cùng transaction (`store.emit_job_events`),
chưa có `seq`. Relay (`relay_service`, một bản trên cụm) gọi `sequence` mỗi giây: khoá dòng
`job_event_counter`, gán `seq` liên tục (last+1, last+2…) theo thứ tự id cho các dòng **đã
commit**, rồi tăng `last`. Vì các lượt gán nối tiếp nhau và mỗi lượt hiện ra cùng lúc khi commit,
người đọc `seq > cursor` không bao giờ thấy seq lớn mà thiếu seq nhỏ hơn — kể cả khi transaction
ghi sự kiện commit lệch thứ tự id (id cấp lúc INSERT). Khoá là khoá dòng nên chạy được sau
PgBouncer chế độ transaction.

Đọc: `changes` (feed theo cursor = seq, quét tối đa `SCAN` seq mỗi lần nên client ít doanh
nghiệp không quét lại cả đuôi), `snapshot` (việc làm hiện tại theo id, trang đầu trả `feed_cursor`
để nối sang feed). Payload là trạng thái hiện tại của việc, không phải lúc xảy ra: bên nhận upsert
theo `job.id` và khử trùng theo `seq` (giao ít nhất một lần). `prune` dọn đầu feed, ghi `pruned`:
cursor thấp hơn → `ResyncRequired` (410, đồng bộ lại bằng snapshot).
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import Exists, delete, exists, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute, defer

from jobcrawler.common.schemas import JobEventType, JobStatus
from jobcrawler.database.models import CompanyRef, Job, JobEvent, JobEventCounter
from jobcrawler.modules.integration.integration_schema import (
    IntegrationJobOut,
    JobChangesOut,
    JobEventOut,
    JobSnapshotOut,
)
from jobcrawler.pipeline.store import job_to_out

COUNTER_ID = 1
# Số sự kiện gán seq tối đa mỗi lượt relay.
SEQUENCE_BATCH = 5_000
# Số seq quét tối đa mỗi lần đọc feed (lọc theo doanh nghiệp của client có thể bỏ gần hết).
SCAN = 5_000
PRUNE_BATCH = 10_000

_SEQUENCE = text(
    """
    UPDATE job_events e SET seq = :last + b.rn
    FROM (
        SELECT id, row_number() OVER (ORDER BY id) AS rn
        FROM job_events WHERE seq IS NULL ORDER BY id LIMIT :limit
    ) b
    WHERE e.id = b.id
    """
)


class ResyncRequired(Exception):
    """Cursor ngoài khoảng còn giữ (đã dọn, hoặc lớn hơn seq mới nhất — DB được khôi phục)."""


@dataclass(frozen=True, slots=True)
class Scope:
    """Phạm vi đọc của một client (`api_clients`)."""

    client_id: UUID
    all_companies: bool = False
    include_description: bool = True


def head_key(prefix: str) -> str:
    """Khoá Redis relay ghi seq mới nhất sau mỗi lượt gán (feed long-poll chờ khoá này đổi)."""
    return f"{prefix}:integration:head"


def parse_cursor(value: str | None) -> int:
    """Cursor feed (chuỗi số) → seq; trống = 0 (đầu feed). Sai định dạng → ValueError."""
    if value is None or value == "":
        return 0
    n = int(value)
    if n < 0:
        raise ValueError("cursor âm")
    return n


async def sequence(session: AsyncSession, *, limit: int = SEQUENCE_BATCH) -> tuple[int, int]:
    """Gán seq cho các sự kiện đã commit chưa có seq. Trả (số vừa gán, seq mới nhất). Gọi trong
    transaction riêng; commit để người đọc thấy."""
    last = await session.scalar(
        select(JobEventCounter.last).where(JobEventCounter.id == COUNTER_ID).with_for_update()
    )
    if last is None:  # migration 0007 tạo sẵn dòng này
        raise LookupError("thiếu dòng job_event_counter")
    # Câu lệnh mới (snapshot mới, sau khi đã giữ khoá): thấy mọi dòng đã commit tới lúc này.
    result = await session.execute(_SEQUENCE, {"last": last, "limit": limit})
    assigned = int(getattr(result, "rowcount", 0) or 0)
    if assigned:
        await session.execute(
            update(JobEventCounter)
            .where(JobEventCounter.id == COUNTER_ID)
            .values(last=JobEventCounter.last + assigned)
        )
    return assigned, last + assigned


async def bounds(session: AsyncSession) -> tuple[int, int]:
    """(seq đã dọn tới, seq mới nhất)."""
    row = (
        await session.execute(
            select(JobEventCounter.pruned, JobEventCounter.last).where(
                JobEventCounter.id == COUNTER_ID
            )
        )
    ).one()
    return int(row.pruned), int(row.last)


async def head(session: AsyncSession) -> int:
    return (await bounds(session))[1]


async def changes(
    session: AsyncSession,
    scope: Scope,
    cursor: int,
    *,
    limit: int,
    now: datetime,
    scan: int = SCAN,
) -> JobChangesOut:
    """Sự kiện có seq > `cursor` của phạm vi `scope`, tối đa `limit`."""
    pruned, last = await bounds(session)
    if cursor < pruned or cursor > last:
        raise ResyncRequired(f"cursor {cursor} ngoài khoảng ({pruned}, {last}]")
    upper = min(cursor + scan, last)
    stmt = select(JobEvent).where(JobEvent.seq > cursor, JobEvent.seq <= upper)
    if not scope.all_companies:
        stmt = stmt.where(_linked(scope.client_id, JobEvent.company_id))
    events = list(await session.scalars(stmt.order_by(JobEvent.seq).limit(limit)))
    next_cursor = (events[-1].seq or upper) if len(events) == limit else upper
    jobs = await _jobs(session, scope, {e.job_id for e in events}, now=now)
    return JobChangesOut(
        events=[
            JobEventOut(
                seq=e.seq or 0,
                type=JobEventType(e.type),
                occurred_at=e.created_at,
                job_id=e.job_id,
                company_id=e.company_id,
                job=jobs.get(e.job_id),
            )
            for e in events
        ],
        next_cursor=str(next_cursor),
        has_more=next_cursor < last,
        head=str(last),
    )


async def snapshot(
    session: AsyncSession,
    scope: Scope,
    *,
    company_ids: Collection[UUID] | None = None,
    status: JobStatus | None = JobStatus.ACTIVE,
    after: UUID | None = None,
    limit: int,
    now: datetime,
) -> JobSnapshotOut:
    """Việc làm hiện tại của phạm vi `scope` (lọc thêm `company_ids`, `status`; None = mọi trạng
    thái), theo id tăng dần sau `after`. Trang đầu (`after` None) kèm `feed_cursor` = seq mới nhất
    đọc trước khi đọc việc: thay đổi xảy ra sau đó đều có seq lớn hơn."""
    feed_cursor = str(await head(session)) if after is None else None
    stmt = select(Job.id)
    if company_ids is not None:
        stmt = stmt.where(Job.company_id.in_(list(company_ids)))
    if not scope.all_companies:
        stmt = stmt.where(_linked(scope.client_id, Job.company_id))
    if status is JobStatus.ACTIVE:
        # Như danh sách việc làm: việc quá hạn nộp tính là hết hạn dù dòng còn `active`.
        stmt = stmt.where(
            Job.status == JobStatus.ACTIVE.value,
            (Job.expires_at.is_(None)) | (Job.expires_at >= now),
        )
    elif status is not None:
        stmt = stmt.where(Job.status == status.value)
    if after is not None:
        stmt = stmt.where(Job.id > after)
    ids = list(await session.scalars(stmt.order_by(Job.id).limit(limit)))
    jobs = await _jobs(session, scope, ids, now=now)
    data = [jobs[i] for i in ids if i in jobs]
    return JobSnapshotOut(
        data=data,
        next_cursor=str(ids[-1]) if len(ids) == limit else None,
        feed_cursor=feed_cursor,
    )


async def prune(session: AsyncSession, *, before: datetime, batch: int = PRUNE_BATCH) -> int:
    """Xoá tối đa `batch` sự kiện đã gán seq tạo trước `before` (đoạn đầu feed liên tục, tới seq
    lớn nhất trong số đó) và ghi mốc `pruned`. Sự kiện chưa gán seq không bao giờ bị xoá. Trả số
    dòng đã xoá; gọi lại (transaction mới) tới khi < `batch`."""
    upto = await session.scalar(
        select(func.max(JobEvent.seq)).where(
            JobEvent.seq.is_not(None), JobEvent.created_at < before
        )
    )
    if upto is None:
        return 0
    await session.execute(
        update(JobEventCounter)
        .where(JobEventCounter.id == COUNTER_ID)
        .values(pruned=func.greatest(JobEventCounter.pruned, upto))
    )
    chunk = (
        select(JobEvent.id)
        .where(JobEvent.seq <= upto)
        .order_by(JobEvent.seq)
        .limit(batch)
        .scalar_subquery()
    )
    result = await session.execute(delete(JobEvent).where(JobEvent.id.in_(chunk)))
    return int(getattr(result, "rowcount", 0) or 0)


def _linked(client_id: UUID, company_id: InstrumentedAttribute[UUID]) -> Exists:
    """Điều kiện: doanh nghiệp `company_id` có mã bên ngoài của client (EXISTS, không nhân dòng
    khi nhiều mã cùng trỏ một doanh nghiệp)."""
    return exists().where(CompanyRef.client_id == client_id, CompanyRef.company_id == company_id)


async def _jobs(
    session: AsyncSession, scope: Scope, ids: Collection[UUID], *, now: datetime
) -> dict[UUID, IntegrationJobOut]:
    """Trạng thái hiện tại của các việc `ids` (việc đã xoá không có trong kết quả)."""
    if not ids:
        return {}
    stmt = select(Job).where(Job.id.in_(list(ids)))
    if not scope.include_description:
        stmt = stmt.options(
            defer(Job.description_md, raiseload=True),
            defer(Job.requirements_md, raiseload=True),
            defer(Job.benefits_md, raiseload=True),
        )
    rows: Sequence[Job] = list(await session.scalars(stmt))
    refs: dict[UUID, list[str]] = {}
    if rows:
        result = await session.execute(
            select(CompanyRef.company_id, CompanyRef.external_id)
            .where(
                CompanyRef.client_id == scope.client_id,
                CompanyRef.company_id.in_({r.company_id for r in rows}),
            )
            .order_by(CompanyRef.external_id)
        )
        for company_id, external_id in result:
            refs.setdefault(company_id, []).append(external_id)
    out: dict[UUID, IntegrationJobOut] = {}
    for row in rows:
        base = job_to_out(row, now=now, content=scope.include_description)
        out[row.id] = IntegrationJobOut.model_validate(
            {
                **base.model_dump(),
                "id": row.id,
                "company_external_ids": refs.get(row.company_id, []),
                "updated_at": row.updated_at,
            }
        )
    return out
