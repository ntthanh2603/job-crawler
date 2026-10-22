"""Số đo lời gọi LLM cho trang Tổng quan.

Mỗi lời gọi `LLMClient.structured` (kể cả câu trả lời lấy từ cache) cộng vào một hash Redis theo
giờ trên Redis chính, như số đo SLA poll: số lời gọi, thành công, lấy từ cache, lỗi theo loại, số
lần bị 429, token, histogram thời gian; cùng các số đó theo prompt. Mọi process cộng dồn chung,
giữ `TTL` để cửa sổ 30 ngày của trang Tổng quan đọc được. Ghi lỗi chỉ log, không làm hỏng lời gọi.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from redis.asyncio import Redis

from jobcrawler.common.logging import get_logger
from jobcrawler.services.llm import LIMITER_KEY, LLMResult
from jobcrawler.services.net.ratelimit import pause_key

if TYPE_CHECKING:
    from jobcrawler.common.config import Settings

log = get_logger(__name__)

TTL = 31 * 86_400
RETENTION_HOURS = 30 * 24
CHART_HOURS = 24
# Ngưỡng (giây) của histogram thời gian một lời gọi (gồm cả lúc chờ lượt / chờ sau 429).
DURATION_EDGES = (0.5, 1, 2, 3, 5, 8, 12, 20, 30, 45, 60, 90)


def hour_key(prefix: str, hour: int) -> str:
    return f"{prefix}:llm:h:{hour}"


def error_kind(error: str) -> str:
    """Loại lỗi từ `LLMResult.error`: "http_429: …" → "http_429", "rate_limited: chờ…" →
    "rate_limited"."""
    return error.split(":", 1)[0].strip() or "error"


def _bucket(value: float, edges: Sequence[float]) -> int:
    for i, edge in enumerate(edges):
        if value <= edge:
            return i
    return len(edges)


def percentile(counts: Sequence[int], edges: Sequence[float], q: float) -> float | None:
    """Phân vị `q` (0..1) từ histogram: ngưỡng trên của ô chứa phân vị; ô cuối = ngưỡng lớn nhất
    × 2."""
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


class LLMMetrics:
    """Ghi số đo lời gọi LLM lên Redis chính (khoá `<prefix>:llm:h:<giờ epoch>`)."""

    def __init__(self, redis: Redis, *, prefix: str) -> None:
        self._redis = redis
        self._prefix = prefix

    async def record(self, result: LLMResult[Any], *, now: float | None = None) -> None:
        key = hour_key(self._prefix, int((now or time.time()) // 3600))
        p = f"p:{result.prompt}:"
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                pipe.hincrby(key, "n", 1)
                pipe.hincrby(key, f"{p}n", 1)
                if not result.unknown:
                    pipe.hincrby(key, "ok", 1)
                    pipe.hincrby(key, f"{p}ok", 1)
                if result.cached:
                    pipe.hincrby(key, "cached", 1)
                    pipe.hincrby(key, f"{p}cached", 1)
                else:
                    pipe.hincrby(key, "ms", result.duration_ms)
                    pipe.hincrby(key, f"{p}ms", result.duration_ms)
                    pipe.hincrby(key, f"d{_bucket(result.duration_ms / 1000, DURATION_EDGES)}", 1)
                    pipe.hincrby(key, "att", result.attempts)
                if result.throttled:
                    pipe.hincrby(key, "thr", result.throttled)
                if result.error and result.unknown:
                    pipe.hincrby(key, f"e:{error_kind(result.error)}", 1)
                for name, tokens in (
                    ("pt", result.prompt_tokens),
                    ("ct", result.completion_tokens),
                ):
                    if tokens:
                        pipe.hincrby(key, name, tokens)
                        pipe.hincrby(key, f"{p}{name}", tokens)
                pipe.expire(key, TTL)
                await pipe.execute()
        except Exception as exc:  # số đo mất được; lời gọi LLM vẫn trả kết quả
            log.warning("llm.metrics_failed", error=f"{type(exc).__name__}: {exc}")


@dataclass(slots=True)
class LLMPromptStats:
    prompt: str
    calls: int = 0
    ok: int = 0
    cached: int = 0
    avg_ms: int | None = None  # trên các lời gọi thật (không tính cache)
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass(slots=True)
class LLMHour:
    hour: datetime
    ok: int = 0  # gọi LLM, trả JSON hợp lệ
    cached: int = 0
    failed: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass(slots=True)
class LLMStats:
    since: datetime  # đầu giờ của ô cũ nhất trong cửa sổ
    enabled: bool
    model: str | None
    requests_per_minute: float | None
    max_concurrency: int
    timeout_seconds: float
    # Cả cụm tạm dừng gọi LLM tới lúc này (429 vì vượt request/phút); lúc đọc còn tạm dừng mới có.
    # Là mốc giờ, không phải số giây còn lại: trang Tổng quan cache số liệu 60 giây.
    paused_until: datetime | None
    calls: int = 0  # gồm cả câu trả lời lấy từ cache
    ok: int = 0
    cached: int = 0
    failed: int = 0
    throttled: int = 0  # số phản hồi 429
    attempts: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    avg_ms: int | None = None
    p50_seconds: float | None = None
    p95_seconds: float | None = None
    per_hour: float | None = None
    errors: list[tuple[str, int]] = field(default_factory=list)
    prompts: list[LLMPromptStats] = field(default_factory=list)
    chart: list[LLMHour] = field(default_factory=list)  # 24 giờ gần nhất


def _decode(row: dict[Any, Any]) -> dict[str, int]:
    return {(k.decode() if isinstance(k, bytes) else k): int(v) for k, v in row.items()}


async def collect(
    redis: Redis, settings: Settings, *, hours: float | None, now: float | None = None
) -> LLMStats:
    """Số đo trong `hours` giờ gần nhất (None = toàn bộ thời gian giữ, `RETENTION_HOURS`), làm
    tròn xuống đầu giờ; kèm biểu đồ 24 giờ và trạng thái tạm dừng hiện tại."""
    now = now or time.time()
    end = int(now // 3600)
    span = min(hours or RETENTION_HOURS, RETENTION_HOURS)
    start = min(int((now - span * 3600) // 3600), end)
    first = min(start, end - CHART_HOURS + 1)
    prefix = settings.redis_prefix
    async with redis.pipeline(transaction=False) as pipe:
        for h in range(first, end + 1):
            pipe.hgetall(hour_key(prefix, h))
        pipe.pttl(pause_key(prefix, LIMITER_KEY))
        *rows, paused_ms = await pipe.execute()

    st = LLMStats(
        since=datetime.fromtimestamp(start * 3600, UTC),
        enabled=settings.llm_enabled,
        model=settings.llm_model,
        requests_per_minute=settings.llm_requests_per_minute,
        max_concurrency=settings.llm_max_concurrency,
        timeout_seconds=settings.llm_timeout_seconds,
        paused_until=datetime.fromtimestamp(now + int(paused_ms) / 1000, UTC)
        if int(paused_ms) > 0
        else None,
    )
    durations = [0] * (len(DURATION_EDGES) + 1)
    total_ms = 0
    errors: dict[str, int] = {}
    prompts: dict[str, dict[str, int]] = {}
    for h, row in zip(range(first, end + 1), rows, strict=True):
        data = _decode(row)
        n, ok, cached = data.get("n", 0), data.get("ok", 0), data.get("cached", 0)
        if h > end - CHART_HOURS:
            st.chart.append(
                LLMHour(
                    hour=datetime.fromtimestamp(h * 3600, UTC),
                    ok=ok - cached,
                    cached=cached,
                    failed=n - ok,
                    prompt_tokens=data.get("pt", 0),
                    completion_tokens=data.get("ct", 0),
                )
            )
        if h < start:
            continue
        st.calls += n
        st.ok += ok
        st.cached += cached
        st.throttled += data.get("thr", 0)
        st.attempts += data.get("att", 0)
        st.prompt_tokens += data.get("pt", 0)
        st.completion_tokens += data.get("ct", 0)
        total_ms += data.get("ms", 0)
        for i in range(len(durations)):
            durations[i] += data.get(f"d{i}", 0)
        for name, value in data.items():
            if name.startswith("e:"):
                errors[name[2:]] = errors.get(name[2:], 0) + value
            elif name.startswith("p:"):
                prompt, _, metric = name[2:].rpartition(":")
                bucket = prompts.setdefault(prompt, {})
                bucket[metric] = bucket.get(metric, 0) + value

    st.failed = st.calls - st.ok
    called = st.calls - st.cached
    st.avg_ms = round(total_ms / called) if called else None
    st.p50_seconds = percentile(durations, DURATION_EDGES, 0.5)
    st.p95_seconds = percentile(durations, DURATION_EDGES, 0.95)
    hours_seen = (now - start * 3600) / 3600
    st.per_hour = round(st.calls / hours_seen, 1) if st.calls and hours_seen > 0 else None
    st.errors = sorted(errors.items(), key=lambda kv: -kv[1])
    for prompt, m in sorted(prompts.items(), key=lambda kv: -kv[1].get("n", 0)):
        real = m.get("n", 0) - m.get("cached", 0)
        st.prompts.append(
            LLMPromptStats(
                prompt=prompt,
                calls=m.get("n", 0),
                ok=m.get("ok", 0),
                cached=m.get("cached", 0),
                avg_ms=round(m.get("ms", 0) / real) if real else None,
                prompt_tokens=m.get("pt", 0),
                completion_tokens=m.get("ct", 0),
            )
        )
    return st
