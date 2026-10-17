"""robots.txt theo RFC 9309 (khớp dài nhất, wildcard `*` và `$`) + `Crawl-delay`.

Tự viết thay cho `urllib.robotparser` vì bản chuẩn không hỗ trợ wildcard trong đường dẫn và
dùng luật khớp đầu tiên thay vì khớp dài nhất.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlsplit

from jobcrawler.net.kv import KeyValueStore

MAX_ROBOTS_BYTES = 500 * 1024
# Lỗi server/mạng khi đọc robots.txt: coi như cấm toàn bộ, nhưng chỉ cache ngắn.
ERROR_CACHE_SECONDS = 600

RobotsFetch = Callable[[str], Awaitable[tuple[int | None, str]]]


def _normalize_path(path: str) -> str:
    """Chuẩn hoá percent-encoding để so khớp ổn định (giữ nguyên ký tự đặc biệt của URL)."""
    return quote(unquote(path), safe="/?=&*$:@!,;+~%-._")


@dataclass(frozen=True, slots=True)
class _Rule:
    allow: bool
    pattern: str
    regex: re.Pattern[str]

    @classmethod
    def compile(cls, allow: bool, pattern: str) -> _Rule:
        pattern = _normalize_path(pattern)
        anchored = pattern.endswith("$")
        body = pattern[:-1] if anchored else pattern
        regex = re.escape(body).replace(r"\*", ".*") + ("$" if anchored else "")
        return cls(allow=allow, pattern=pattern, regex=re.compile(regex))


@dataclass(slots=True)
class RobotsRules:
    rules: list[_Rule] = field(default_factory=list)
    crawl_delay: float | None = None
    sitemaps: list[str] = field(default_factory=list)
    disallow_all: bool = False
    # Cấm toàn bộ vì không tải được robots.txt (lỗi mạng, 5xx, 429), không phải vì luật.
    unreachable: bool = False

    @classmethod
    def allow_everything(cls) -> RobotsRules:
        return cls()

    @classmethod
    def deny_everything(cls) -> RobotsRules:
        return cls(disallow_all=True)

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        path = parts.path or "/"
        if path == "/robots.txt":
            return True
        if self.disallow_all:
            return False
        target = _normalize_path(path + (f"?{parts.query}" if parts.query else ""))
        best: _Rule | None = None
        for rule in self.rules:
            if not rule.regex.match(target):
                continue
            # Khớp dài nhất thắng; bằng nhau thì Allow thắng.
            if (
                best is None
                or len(rule.pattern) > len(best.pattern)
                or (len(rule.pattern) == len(best.pattern) and rule.allow and not best.allow)
            ):
                best = rule
        return best is None or best.allow


def parse_robots(text: str, user_agents: Sequence[str] = ("*",)) -> RobotsRules:
    """Lấy luật của các nhóm khớp `user_agents` (không phân biệt hoa thường); không có thì `*`."""
    wanted = {ua.lower() for ua in user_agents if ua != "*"}
    groups: list[tuple[set[str], list[tuple[str, str]]]] = []
    sitemaps: list[str] = []
    current_agents: set[str] = set()
    current_lines: list[tuple[str, str]] = []
    in_agents = False

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (s.strip() for s in line.split(":", 1))
        key = key.lower()
        if key == "sitemap":
            if value:
                sitemaps.append(value)
            continue
        if key == "user-agent":
            if not in_agents:
                if current_agents:
                    groups.append((current_agents, current_lines))
                current_agents, current_lines = set(), []
            current_agents.add(value.lower())
            in_agents = True
            continue
        if key in {"allow", "disallow", "crawl-delay"}:
            in_agents = False
            if current_agents:
                current_lines.append((key, value))
    if current_agents:
        groups.append((current_agents, current_lines))

    selected = [lines for agents, lines in groups if agents & wanted]
    if not selected:
        selected = [lines for agents, lines in groups if "*" in agents]

    rules = RobotsRules(sitemaps=sitemaps)
    for lines in selected:
        for key, value in lines:
            if key == "crawl-delay":
                try:
                    delay = float(value)
                except ValueError:
                    continue
                if delay > 0:
                    rules.crawl_delay = max(rules.crawl_delay or 0.0, delay)
            elif value:  # `Disallow:` rỗng = không cấm gì
                rules.rules.append(_Rule.compile(key == "allow", value))
    return rules


def robots_url(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/robots.txt"


class RobotsCache:
    """robots.txt theo origin: cache thô trong KV (mặc định 24h) + bản đã parse trong process."""

    def __init__(
        self,
        kv: KeyValueStore,
        fetch: RobotsFetch,
        *,
        ttl: float = 86_400,
        user_agents: Sequence[str] = ("*",),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._kv = kv
        self._fetch = fetch
        self._ttl = ttl
        self._user_agents = tuple(user_agents)
        self._clock = clock
        self._local: dict[str, tuple[float, RobotsRules]] = {}

    def _from_response(self, status: int | None, text: str) -> tuple[RobotsRules, float]:
        if status is not None and 200 <= status < 300:
            return parse_robots(text[:MAX_ROBOTS_BYTES], self._user_agents), self._ttl
        if status is not None and 400 <= status < 500 and status != 429:
            # RFC 9309: robots.txt không có (4xx) → được crawl toàn bộ.
            return RobotsRules.allow_everything(), self._ttl
        # 5xx, 429, lỗi mạng → tạm coi như cấm toàn bộ (RFC 9309 mục 2.3.1.4).
        return RobotsRules(disallow_all=True, unreachable=True), min(self._ttl, ERROR_CACHE_SECONDS)

    async def rules_for(self, url: str) -> RobotsRules:
        origin = robots_url(url)
        now = self._clock()
        cached = self._local.get(origin)
        if cached is not None and cached[0] > now:
            return cached[1]

        key = f"robots:{origin}"
        raw = await self._kv.get(key)
        if raw is not None:
            data = json.loads(raw)
            rules, ttl = self._from_response(data["status"], data["text"])
        else:
            status, text = await self._fetch(origin)
            rules, ttl = self._from_response(status, text)
            payload = json.dumps({"status": status, "text": text[:MAX_ROBOTS_BYTES]})
            await self._kv.set(key, payload, ttl=ttl)

        if len(self._local) > 4096:
            self._local.clear()
        # Bản parse trong process sống ngắn hơn để thấy được cập nhật từ process khác.
        self._local[origin] = (now + min(ttl, 300), rules)
        return rules
