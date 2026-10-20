"""Heartbeat health của process chạy lâu không có cổng để hỏi (scheduler, runner).

Mỗi process ghi khoá `<REDIS_PREFIX>:health:<component>:<hostname>` = mốc `time.time()` (TTL
`HEARTBEAT_TTL`) mỗi `HEARTBEAT_SECONDS` giây, NGAY TRONG vòng lặp chính (không phải luồng / task
riêng): vòng lặp kẹt thì khoá thôi được làm mới. `hostname` = `socket.gethostname()`, trong Docker
là id container, nên healthcheck (`jobcrawler health <component>`, chạy trong chính container đó)
chỉ xem heartbeat của mình, không lẫn với replica khác.

Component: `scheduler`; runner theo vai: `runner-build`, `runner-poll`, `runner-boards`, chạy đủ cả
ba vai trong một process là `runner` (vài vai: `runner-build-poll`...). Runner nhiều vai chỉ ghi khi
vòng lặp của MỌI vai đã quay kể từ lần ghi trước (`parts`).

Khoá nằm trên Redis chính (REDIS_URL), không phải Redis cache: bị đẩy ra là healthcheck báo hỏng.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Callable, Iterable
from itertools import combinations
from typing import Any

from jobcrawler.logging import get_logger

log = get_logger(__name__)

HEARTBEAT_SECONDS = 5.0
# Khoá tự mất sau chừng này giây (container đã xoá không để lại rác); lớn hơn ngưỡng mặc định của
# healthcheck để `jobcrawler health` báo được "cũ N giây" thay vì chỉ "không có".
HEARTBEAT_TTL = 120
# Ngưỡng mặc định của `jobcrawler health`: lỡ ba nhịp liền mới coi là hỏng.
DEFAULT_MAX_AGE = 3 * HEARTBEAT_SECONDS

RUNNER_ROLES = ("build", "poll", "boards")


def runner_component(roles: Iterable[str]) -> str:
    """Tên component của một process runner: đủ ba vai → `runner`, không thì `runner-<vai>...`
    theo thứ tự build, poll, boards."""
    chosen = set(roles)
    ordered = [r for r in RUNNER_ROLES if r in chosen]
    if not ordered or len(ordered) == len(RUNNER_ROLES):
        return "runner"
    return "runner-" + "-".join(ordered)


def components() -> tuple[str, ...]:
    """Mọi tên component hợp lệ (cho `jobcrawler health`)."""
    runners = {
        runner_component(c)
        for n in range(1, len(RUNNER_ROLES) + 1)
        for c in combinations(RUNNER_ROLES, n)
    }
    return ("scheduler", *sorted(runners, key=lambda c: (len(c), c)))


def heartbeat_key(prefix: str, component: str, host: str | None = None) -> str:
    return f"{prefix}:health:{component}:{host or socket.gethostname()}"


class Heartbeat:
    """Ghi heartbeat của một process. Vòng lặp chính gọi `tick(part)` mỗi lần quay; khoá được ghi
    khi mọi `parts` đã tick kể từ lần ghi trước và đã qua `interval` giây. Redis lỗi chỉ log cảnh
    báo (healthcheck tự thấy heartbeat cũ)."""

    def __init__(
        self,
        redis: Any,
        prefix: str,
        component: str,
        *,
        parts: Iterable[str] = ("main",),
        interval: float = HEARTBEAT_SECONDS,
        ttl: int = HEARTBEAT_TTL,
        host: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._redis = redis
        self.component = component
        self.key = heartbeat_key(prefix, component, host)
        self.parts = frozenset(parts) or frozenset({"main"})
        self.interval = interval
        self.ttl = ttl
        self._clock = clock
        self._waiting = set(self.parts)
        self._last = float("-inf")

    async def tick(self, part: str = "main") -> bool:
        """Vòng lặp `part` vừa quay. Trả True nếu lần này đã ghi khoá."""
        self._waiting.discard(part)
        now = self._clock()
        if self._waiting or now - self._last < self.interval:
            return False
        try:
            await self._redis.set(self.key, f"{now:.3f}", ex=self.ttl)
        except Exception as exc:
            log.warning("health.heartbeat_failed", component=self.component, error=repr(exc))
            return False
        self._last = now
        self._waiting = set(self.parts)
        return True


async def check(
    redis: Any,
    prefix: str,
    component: str,
    *,
    max_age: float = DEFAULT_MAX_AGE,
    host: str | None = None,
    now: float | None = None,
) -> tuple[bool, str]:
    """(khoẻ?, lý do ngắn) của heartbeat `component` trên máy / container này."""
    key = heartbeat_key(prefix, component, host)
    raw = await redis.get(key)
    if raw is None:
        return False, f"không có heartbeat ({key})"
    try:
        beat = float(raw.decode() if isinstance(raw, bytes) else raw)
    except ValueError:
        return False, f"heartbeat hỏng ({key}): {raw!r}"
    age = (now if now is not None else time.time()) - beat
    if age > max_age:
        return False, f"heartbeat cũ {age:.1f} s > {max_age:g} s ({key})"
    return True, f"ok {component} ({age:.1f} s trước)"
