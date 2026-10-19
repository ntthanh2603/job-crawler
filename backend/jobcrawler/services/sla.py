"""Số đo độ trễ phát hiện tin mới (mục tiêu P95 ≤ 3 phút).

Một tin đăng ngay sau lúc lượt poll trước BẮT ĐẦU sẽ được thấy muộn nhất khi lượt poll kế tiếp
XONG. Nên với mỗi lượt poll đủ, "cửa sổ phát hiện" = lúc xong − lúc bắt đầu lượt poll đủ trước:
P95 của cửa sổ này là P95 độ trễ phát hiện trong trường hợp xấu nhất.

Ghi vào Redis theo phút (histogram đếm theo ngưỡng, giữ 3 ngày) để mọi runner cộng dồn được rẻ;
trang tổng quan đọc tổng N phút gần nhất. Kèm ảnh chụp độ tươi hiện tại từ `company_schedules`.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from jobcrawler.db.models import CompanySchedule
from jobcrawler.schemas import ScheduleState

TTL = 3 * 86_400
# Ngưỡng (giây) của histogram: thời gian một lượt poll, cửa sổ phát hiện.
DURATION_EDGES = (0.5, 1, 2, 3, 5, 8, 12, 20, 30, 45, 60, 90)
WINDOW_EDGES = (30, 60, 90, 120, 135, 150, 165, 180, 210, 240, 300, 600, 1800, 3600)
TARGET_SECONDS = 180


def _key(prefix: str, minute: int) -> str:
    return f"{prefix}:sla:poll:{minute}"


def _bucket(value: float, edges: Sequence[float]) -> int:
    for i, edge in enumerate(edges):
        if value <= edge:
            return i
    return len(edges)


async def record_poll(
    redis: Redis,
    prefix: str,
    *,
    status: str,
    duration_s: float,
    window_s: float | None,
    new_jobs: int = 0,
    changed: bool = False,
    now: float | None = None,
) -> None:
    """Ghi một lượt poll. `status`: ok | failed | broken | skipped. `window_s`: cửa sổ phát hiện
    (None nếu chưa có lượt poll đủ trước)."""
    key = _key(prefix, int((now or time.time()) // 60))
    async with redis.pipeline(transaction=False) as pipe:
        pipe.hincrby(key, "n", 1)
        pipe.hincrby(key, status, 1)
        if status == "ok":
            pipe.hincrby(key, f"d{_bucket(duration_s, DURATION_EDGES)}", 1)
            if window_s is not None:
                pipe.hincrby(key, f"w{_bucket(window_s, WINDOW_EDGES)}", 1)
        if changed:
            pipe.hincrby(key, "changed", 1)
        if new_jobs:
            pipe.hincrby(key, "new_jobs", new_jobs)
        pipe.expire(key, TTL)
        await pipe.execute()


def percentile(counts: Sequence[int], edges: Sequence[float], q: float) -> float | None:
    """Phân vị `q` (0..1) từ histogram: ngưỡng trên của ô chứa phân vị (bảo thủ). Ô cuối (quá
    ngưỡng lớn nhất) trả ngưỡng lớn nhất × 2."""
    total = sum(counts)
    if not total:
        return None
    need = q * total
    seen = 0
    for i, n in enumerate(counts):
        seen += n
        if seen >= need:
            return float(edges[i]) if i < len(edges) else float(edges[-1]) * 2
    return float(edges[-1]) * 2


@dataclass(slots=True)
class PollStats:
    minutes: int
    polls: int = 0
    ok: int = 0
    failed: int = 0
    broken: int = 0
    skipped: int = 0
    changed: int = 0
    new_jobs: int = 0
    per_second: float = 0.0
    duration_p50: float | None = None
    duration_p95: float | None = None
    window_p50: float | None = None
    window_p95: float | None = None
    within_target: float | None = None  # tỉ lệ cửa sổ phát hiện ≤ 3 phút
    window_histogram: list[int] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


async def poll_stats(
    redis: Redis, prefix: str, *, minutes: int = 60, now: float | None = None
) -> PollStats:
    end = int((now or time.time()) // 60)
    keys = [_key(prefix, m) for m in range(end - minutes + 1, end + 1)]
    async with redis.pipeline(transaction=False) as pipe:
        for key in keys:
            pipe.hgetall(key)
        rows: list[dict[Any, Any]] = await pipe.execute()
    st = PollStats(minutes=minutes)
    durations = [0] * (len(DURATION_EDGES) + 1)
    windows = [0] * (len(WINDOW_EDGES) + 1)
    for row in rows:
        data = {(k.decode() if isinstance(k, bytes) else k): int(v) for k, v in row.items()}
        st.polls += data.get("n", 0)
        st.ok += data.get("ok", 0)
        st.failed += data.get("failed", 0)
        st.broken += data.get("broken", 0)
        st.skipped += data.get("skipped", 0)
        st.changed += data.get("changed", 0)
        st.new_jobs += data.get("new_jobs", 0)
        for i in range(len(durations)):
            durations[i] += data.get(f"d{i}", 0)
        for i in range(len(windows)):
            windows[i] += data.get(f"w{i}", 0)
    st.per_second = round(st.polls / (minutes * 60), 2)
    st.duration_p50 = percentile(durations, DURATION_EDGES, 0.5)
    st.duration_p95 = percentile(durations, DURATION_EDGES, 0.95)
    st.window_p50 = percentile(windows, WINDOW_EDGES, 0.5)
    st.window_p95 = percentile(windows, WINDOW_EDGES, 0.95)
    if sum(windows):
        within = sum(windows[: _bucket(TARGET_SECONDS, WINDOW_EDGES) + 1])
        st.within_target = round(within / sum(windows), 4)
    st.window_histogram = windows
    return st


@dataclass(slots=True)
class Freshness:
    """Ảnh chụp lúc này: doanh nghiệp đang poll và tuổi lượt poll đủ gần nhất của chúng."""

    ready: int = 0
    by_state: dict[str, int] = field(default_factory=dict)
    age_p50: float | None = None
    age_p95: float | None = None
    age_max: float | None = None
    overdue: int = 0  # quá hạn poll hơn 1 phút
    never_polled: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


async def freshness(session: AsyncSession, now: datetime | None = None) -> Freshness:
    now = now or datetime.now(UTC)
    S = CompanySchedule
    out = Freshness()
    rows = await session.execute(select(S.state, func.count()).group_by(S.state))
    out.by_state = {state: n for state, n in rows}
    out.ready = out.by_state.get(ScheduleState.READY.value, 0)
    age = func.extract("epoch", now - S.last_poll_ok_at)
    row = (
        await session.execute(
            select(
                func.percentile_cont(0.5).within_group(age),
                func.percentile_cont(0.95).within_group(age),
                func.max(age),
                func.count().filter(S.next_poll_at < now - func.make_interval(0, 0, 0, 0, 0, 1)),
                func.count().filter(S.last_poll_ok_at.is_(None)),
            ).where(S.state == ScheduleState.READY.value)
        )
    ).one()
    p50, p95, oldest, overdue, never = row
    out.age_p50 = round(float(p50), 1) if p50 is not None else None
    out.age_p95 = round(float(p95), 1) if p95 is not None else None
    out.age_max = round(float(oldest), 1) if oldest is not None else None
    out.overdue, out.never_polled = int(overdue or 0), int(never or 0)
    return out
