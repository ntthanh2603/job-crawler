"""Khung giờ build/poll và cách rải doanh nghiệp (modules/crawl/schedule_service.py)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from jobcrawler.common.config import Settings
from jobcrawler.database.ids import uuid7
from jobcrawler.modules.crawl import schedule_service
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
