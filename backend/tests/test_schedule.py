"""Khung giờ build/poll và cách rải doanh nghiệp (modules/crawl/schedule_service.py)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.dialects import postgresql

from jobcrawler.common.config import Settings
from jobcrawler.common.schemas import RunTrigger
from jobcrawler.database.ids import uuid7
from jobcrawler.modules.crawl import crawl_service, schedule_service
from jobcrawler.modules.crawl.schedule_service import Window

pytestmark = pytest.mark.usefixtures("clean_env")

VN = "Asia/Ho_Chi_Minh"


def vn(hour: int, minute: int = 0, day: int = 19) -> datetime:
    """Giờ Việt Nam (UTC+7) ngày `day`/10/2026 → UTC."""
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC) - timedelta(hours=7)


@pytest.mark.parametrize(
    ("window", "hour", "inside"),
    [
        ("00:00-06:00", 0, True),
        ("00:00-06:00", 5, True),
        ("00:00-06:00", 6, False),
        ("06:00-24:00", 23, True),
        ("06:00-24:00", 3, False),
        ("22:00-05:00", 23, True),
        ("22:00-05:00", 2, True),
        ("22:00-05:00", 12, False),
        ("00:00-00:00", 12, True),  # bắt đầu = kết thúc: cả ngày
    ],
)
def test_window_contains(window: str, hour: int, inside: bool) -> None:
    assert Window(window, VN).contains(vn(hour, 30)) is inside


def test_window_starts() -> None:
    night = Window("00:00-06:00", VN)
    assert night.minutes == 360
    assert night.current_start(vn(2, 15)) == vn(0, 0)
    assert night.current_start(vn(12)) is None
    assert night.next_start(vn(2)) == vn(0, 0, day=20)
    assert night.next_start(vn(23)) == vn(0, 0, day=20)
    wrap = Window("22:00-05:00", VN)
    assert wrap.current_start(vn(3)) == vn(22, 0, day=18)
    assert wrap.next_start(vn(12)) == vn(22)


def test_phase_spreads_companies_uniformly() -> None:
    phases = [schedule_service.phase(uuid7()) for _ in range(4000)]
    assert all(0 <= p < 1 for p in phases)
    # UUIDv7 sinh liền nhau gần như cùng tiền tố thời gian: hash vẫn phải rải đều.
    quarters = [sum(q / 4 <= p < (q + 1) / 4 for p in phases) for q in range(4)]
    assert min(quarters) > 850


def test_next_build_at_is_inside_tonights_window() -> None:
    settings = Settings(build_window="00:00-06:00", crawl_timezone=VN)
    cid = uuid4()
    at = schedule_service.next_build_at(cid, vn(14), settings)
    assert vn(0, day=20) <= at < vn(6, day=20)
    # Đang trong khung, chỗ của doanh nghiệp còn ở phía trước → đêm nay; đã qua → đêm mai.
    slot = at - timedelta(days=1)
    assert schedule_service.next_build_at(cid, slot - timedelta(minutes=1), settings) == slot
    assert schedule_service.next_build_at(cid, slot + timedelta(minutes=1), settings) == at


def test_first_poll_spreads_within_one_interval() -> None:
    settings = Settings(poll_window="06:00-24:00", poll_interval_seconds=150, crawl_timezone=VN)
    now = vn(9)
    for _ in range(50):
        at = schedule_service.first_poll_at(uuid4(), now, settings)
        assert now <= at < now + timedelta(seconds=150)
    # Ngoài khung poll: chu kỳ đầu của khung kế tiếp.
    early = schedule_service.first_poll_at(uuid4(), vn(3), settings)
    assert vn(6) <= early < vn(6) + timedelta(seconds=150)


def test_ten_thousand_companies_fit_the_night_and_the_poll_cycle() -> None:
    """Rải 10.000 doanh nghiệp: build đêm ≤ ~1 lượt/giây ở mọi phút, poll ≈ 67 lượt/giây đều."""
    settings = Settings(build_window="00:00-06:00", poll_interval_seconds=150, crawl_timezone=VN)
    ids = [uuid7() for _ in range(10_000)]
    builds = [schedule_service.next_build_at(cid, vn(12), settings) for cid in ids]
    per_minute: dict[int, int] = {}
    for at in builds:
        minute = int((at - vn(0, day=20)).total_seconds() // 60)
        per_minute[minute] = per_minute.get(minute, 0) + 1
    assert max(per_minute) < 360
    assert max(per_minute.values()) <= 60  # ~28 trung bình mỗi phút
    polls = [schedule_service.first_poll_at(cid, vn(9), settings) for cid in ids]
    per_10s: dict[int, int] = {}
    for at in polls:
        bucket = int((at - vn(9)).total_seconds() // 10)
        per_10s[bucket] = per_10s.get(bucket, 0) + 1
    assert len(per_10s) == 15
    assert max(per_10s.values()) < 1.3 * 10_000 / 15


def sql(stmt: Any) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


class CaptureSession:
    """Phiên giả: ghi câu lệnh, không trả dòng nào."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, stmt: Any) -> Any:
        self.statements.append(stmt)
        return SimpleNamespace(all=list)

    async def scalars(self, stmt: Any) -> list[Any]:
        self.statements.append(stmt)
        return []


async def test_new_schedules_ignore_rows_written_meanwhile() -> None:
    # Lượt build ghi dòng lịch giữa lúc SELECT và INSERT: không vi phạm khoá, nhịp không hỏng.
    session = CaptureSession()
    await schedule_service.ensure_schedules(session, vn(9))  # pyright: ignore[reportArgumentType]
    assert "ON CONFLICT (company_id) DO NOTHING" in sql(session.statements[0])


async def test_new_company_waits_for_its_retry_time() -> None:
    # Lượt build doanh nghiệp mới không xong (job lỗi / bị bỏ): thử lại sau RETRY_BUILD, không xếp
    # lại mỗi nhịp.
    session = CaptureSession()
    await schedule_service.due_builds(session, vn(9), Settings(), 10)  # pyright: ignore[reportArgumentType]
    new_claim = sql(session.statements[0])
    assert "company_schedules.state = " in new_claim
    assert "company_schedules.next_build_at <= " in new_claim


class TickSessions:
    """`sessionmaker.begin()` giả: ghi commit / rollback theo thứ tự cùng các lần xếp job."""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[object]:
        try:
            yield object()
        except BaseException:
            self.events.append("rollback")
            raise
        self.events.append("commit")


@pytest.mark.parametrize("build_error", [False, True])
async def test_tick_enqueues_before_committing(
    monkeypatch: pytest.MonkeyPatch, build_error: bool
) -> None:
    """Mốc đã dời chỉ được commit sau khi job đã vào hàng đợi; xếp build lỗi thì mốc build không
    bị dời (rollback) và lượt poll của nhịp vẫn được xếp."""
    events: list[str] = []
    build_id, poll_id = uuid4(), uuid4()

    async def nothing(*_args: Any) -> int:
        return 0

    async def room(*_args: Any) -> int:
        return 100

    async def builds(*_args: Any) -> list[tuple[UUID, RunTrigger]]:
        return [(build_id, RunTrigger.NIGHTLY)]

    async def polls(*_args: Any) -> list[UUID]:
        return [poll_id]

    async def enqueue_builds(_res: Any, ids: list[UUID], **_kwargs: Any) -> int:
        if build_error:
            raise ConnectionError("redis")
        events.append("enqueue_builds")
        return len(ids)

    async def enqueue_polls(_res: Any, ids: list[UUID], **_kwargs: Any) -> int:
        events.append("enqueue_polls")
        return len(ids)

    monkeypatch.setattr(schedule_service, "ensure_schedules", nothing)
    monkeypatch.setattr(schedule_service, "queue_room", room)
    monkeypatch.setattr(schedule_service, "due_builds", builds)
    monkeypatch.setattr(schedule_service, "due_polls", polls)
    monkeypatch.setattr(crawl_service, "enqueue_builds", enqueue_builds)
    monkeypatch.setattr(crawl_service, "enqueue_polls", enqueue_polls)
    res: Any = SimpleNamespace(
        settings=Settings(),
        sessionmaker=TickSessions(events),
        queues=SimpleNamespace(build=None, poll=None),
    )
    if build_error:
        with pytest.raises(ConnectionError):
            await schedule_service.tick(res, vn(9))
        assert events == ["rollback", "enqueue_polls", "commit"]
    else:
        assert await schedule_service.tick(res, vn(9)) == {"created": 0, "builds": 1, "polls": 1}
        assert events == ["enqueue_builds", "commit", "enqueue_polls", "commit"]
