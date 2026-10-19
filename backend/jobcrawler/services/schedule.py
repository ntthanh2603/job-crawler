"""Lịch thu thập: build bộ khung ban đêm, poll tin mới ban ngày (`company_schedules`).

- Mỗi doanh nghiệp có một "pha" cố định trong [0, 1) (hash của id) để rải đều: build ở
  `đầu BUILD_WINDOW + pha × độ dài khung`, poll đầu ngày ở `đầu POLL_WINDOW + pha × chu kỳ`.
  10.000 doanh nghiệp trong khung đêm 6 giờ ≈ 0,46 lượt build/giây; poll mỗi 150 giây ≈ 67
  lượt/giây — không dồn vào cùng một giây.
- Scheduler (một replica giữ khoá Postgres) mỗi nhịp lấy các dòng đến hạn bằng
  `FOR UPDATE SKIP LOCKED`, dời mốc kế tiếp và xếp job; khoá job theo doanh nghiệp nên job còn
  chờ thì không xếp trùng. Hàng đợi còn nhiều việc chờ thì không xếp thêm (backpressure).
- Mốc poll kế tiếp tính từ lúc xếp (không từ lúc xong) nên chu kỳ giữ đều dù lượt poll dài ngắn.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import DateTime, func, insert, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jobcrawler.config import Settings, parse_window
from jobcrawler.db.models import Company, CompanySchedule
from jobcrawler.logging import get_logger
from jobcrawler.resources import Resources
from jobcrawler.schemas import RunTrigger, ScheduleState

log = get_logger(__name__)

DAY = 24 * 60
# Build lỗi / doanh nghiệp mới: thử lại sau chừng này nếu lượt build không tự đặt lịch (runner
# chết, job bị bỏ).
RETRY_BUILD = timedelta(minutes=30)
# Bộ khung hỏng mà build lại vẫn hỏng: chờ chừng này mới build lại (không build liên tục).
BROKEN_BACKOFF = timedelta(hours=2)


def phase(company_id: UUID) -> float:
    """Pha cố định của doanh nghiệp trong [0, 1)."""
    digest = hashlib.blake2b(company_id.bytes, digest_size=8).digest()
    return int.from_bytes(digest, "big") / 2**64


class Window:
    """Khung giờ trong ngày theo múi giờ CRAWL_TIMEZONE ("HH:MM-HH:MM", qua nửa đêm được)."""

    def __init__(self, value: str, tz: str) -> None:
        self.start, self.end = parse_window(value)
        self.tz = ZoneInfo(tz)

    @property
    def minutes(self) -> int:
        """Độ dài khung (phút); bắt đầu = kết thúc là cả ngày."""
        length = (self.end - self.start) % DAY
        return length or DAY

    def _local(self, now: datetime) -> datetime:
        return now.astimezone(self.tz)

    def _minute_of_day(self, now: datetime) -> float:
        local = self._local(now)
        return local.hour * 60 + local.minute + local.second / 60

    def contains(self, now: datetime) -> bool:
        if self.minutes == DAY:
            return True
        return (self._minute_of_day(now) - self.start) % DAY < self.minutes

    def current_start(self, now: datetime) -> datetime | None:
        """Lúc bắt đầu của khung đang mở (None nếu đang ngoài khung)."""
        if not self.contains(now):
            return None
        elapsed = (self._minute_of_day(now) - self.start) % DAY
        return (now - timedelta(minutes=elapsed)).replace(microsecond=0).astimezone(UTC)

    def next_start(self, now: datetime) -> datetime:
        """Lần bắt đầu khung kế tiếp, sau `now` (không tính khung đang mở)."""
        local = self._local(now)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
            minutes=self.start
        )
        while start <= local:
            start += timedelta(days=1)
        return start.astimezone(UTC)


def build_window(settings: Settings) -> Window:
    return Window(settings.build_window, settings.crawl_timezone)


def poll_window(settings: Settings) -> Window:
    return Window(settings.poll_window, settings.crawl_timezone)


def next_build_at(company_id: UUID, now: datetime, settings: Settings) -> datetime:
    """Mốc build đêm kế tiếp của doanh nghiệp: chỗ của nó trong khung build, sau `now`."""
    window = build_window(settings)
    offset = timedelta(minutes=phase(company_id) * window.minutes)
    start = window.current_start(now)
    if start is not None and start + offset > now:
        return start + offset
    return window.next_start(now) + offset


def first_poll_at(company_id: UUID, now: datetime, settings: Settings) -> datetime:
    """Lượt poll đầu sau khi có bộ khung: trong khung poll thì rải trong một chu kỳ từ bây giờ,
    ngoài khung thì rải trong chu kỳ đầu của khung poll kế tiếp."""
    offset = timedelta(seconds=phase(company_id) * settings.poll_interval_seconds)
    if poll_window(settings).contains(now):
        return now + offset
    return poll_window(settings).next_start(now) + offset


# --- scheduler --------------------------------------------------------------------------------


async def ensure_schedules(session: AsyncSession, now: datetime) -> int:
    """Tạo dòng lịch cho doanh nghiệp chưa có (mới nạp): trạng thái `new` → build ngay."""
    missing = select(
        Company.id, literal(ScheduleState.NEW.value), literal(now, DateTime(timezone=True))
    ).where(
        ~select(CompanySchedule.company_id).where(CompanySchedule.company_id == Company.id).exists()
    )
    result = await session.execute(
        insert(CompanySchedule)
        .from_select(["company_id", "state", "next_build_at"], missing)
        .returning(CompanySchedule.company_id)
    )
    return len(result.all())


async def _claim(
    session: AsyncSession, where: Sequence[object], order: object, limit: int
) -> list[UUID]:
    if limit <= 0:
        return []
    rows = await session.scalars(
        select(CompanySchedule.company_id)
        .where(*where)  # pyright: ignore[reportArgumentType]
        .order_by(order)  # pyright: ignore[reportArgumentType]
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    return list(rows)


async def due_builds(
    session: AsyncSession, now: datetime, settings: Settings, limit: int
) -> list[tuple[UUID, RunTrigger]]:
    """Doanh nghiệp đến lượt build, kèm lý do; dời `next_build_at` để không xếp lại ngay
    (lượt build xong sẽ đặt mốc đúng)."""
    out: list[tuple[UUID, RunTrigger]] = []
    S = CompanySchedule
    # Mới nạp: build ngay (cần bộ khung để poll), bất kể khung giờ.
    for cid in await _claim(session, [S.state == ScheduleState.NEW.value], S.next_build_at, limit):
        out.append((cid, RunTrigger.NEW))
    # Bộ khung hỏng, lượt build lại trước chưa thành: thử lại khi tới mốc.
    broken = await _claim(
        session,
        [S.state == ScheduleState.BROKEN.value, S.next_build_at <= now],
        S.next_build_at,
        limit - len(out),
    )
    out += [(cid, RunTrigger.ERROR) for cid in broken]
    if build_window(settings).contains(now):
        nightly = await _claim(
            session,
            [
                S.state.in_([ScheduleState.READY.value, ScheduleState.NO_SOURCES.value]),
                S.next_build_at <= now,
            ],
            S.next_build_at,
            limit - len(out),
        )
        out += [(cid, RunTrigger.NIGHTLY) for cid in nightly]
    for cid, trigger in out:
        later = (
            next_build_at(cid, now + timedelta(minutes=1), settings)
            if trigger is RunTrigger.NIGHTLY
            else now + RETRY_BUILD
        )
        await session.execute(update(S).where(S.company_id == cid).values(next_build_at=later))
    return out


async def due_polls(
    session: AsyncSession, now: datetime, settings: Settings, limit: int
) -> list[UUID]:
    """Doanh nghiệp đến lượt poll (trong khung poll), dời `next_poll_at` thêm một chu kỳ ± lệch
    ngẫu nhiên. Một câu lệnh: UPDATE … WHERE company_id IN (SELECT … FOR UPDATE SKIP LOCKED)."""
    if limit <= 0 or not poll_window(settings).contains(now):
        return []
    S = CompanySchedule
    due = (
        select(S.company_id)
        .where(S.state == ScheduleState.READY.value, S.next_poll_at <= now)
        .order_by(S.next_poll_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    seconds = (
        settings.poll_interval_seconds + (func.random() * 2 - 1) * settings.poll_jitter_seconds
    )
    rows = await session.scalars(
        update(S)
        .where(S.company_id.in_(due))
        .values(
            next_poll_at=literal(now, DateTime(timezone=True))
            + func.make_interval(0, 0, 0, 0, 0, 0, seconds)
        )
        .returning(S.company_id)
    )
    return list(rows)


async def queue_room(queue: object, max_backlog: int) -> int:
    """Số job còn xếp thêm được để hàng đợi không vượt `max_backlog` việc chờ."""
    info = await queue.info()  # pyright: ignore[reportAttributeAccessIssue]
    return max(0, max_backlog - int(info["queued"]) - int(info["scheduled"]))


async def tick(res: Resources, now: datetime | None = None) -> dict[str, int]:
    """Một nhịp scheduler: tạo lịch còn thiếu, xếp build đến hạn, xếp poll đến hạn."""
    from jobcrawler.services import crawl

    s = res.settings
    now = now or datetime.now(UTC)
    async with res.sessionmaker.begin() as session:
        created = await ensure_schedules(session, now)
        room = await queue_room(res.queues.build, s.build_queue_max_backlog)
        builds = await due_builds(session, now, s, min(room, s.scheduler_batch))
    async with res.sessionmaker.begin() as session:
        room = await queue_room(res.queues.poll, s.poll_queue_max_backlog)
        polls = await due_polls(session, now, s, min(room, s.scheduler_batch))
    by_trigger: dict[RunTrigger, list[UUID]] = {}
    for cid, trigger in builds:
        by_trigger.setdefault(trigger, []).append(cid)
    built = 0
    for trigger, ids in by_trigger.items():
        built += await crawl.enqueue_builds(res, ids, trigger=trigger)
    polled = await crawl.enqueue_polls(res, polls, trigger=RunTrigger.SCHEDULE)
    if created or builds or polls:
        log.debug("schedule.tick", created=created, builds=built, polls=polled)
    return {"created": created, "builds": built, "polls": polled}
