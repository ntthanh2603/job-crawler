"""Proxy pool: đọc `proxies.txt`, gắn proxy theo domain (sticky), chấm điểm sức khoẻ.

Trạng thái nằm trong `KeyValueStore` (Redis khi chạy thật) để mọi worker dùng chung:

- `proxy:score:{id}`        điểm 0..100, cao thì hay được chọn hơn;
- `proxy:fails:{id}`        số lỗi kết nối liên tiếp; đủ ngưỡng thì loại (`dead`);
- `proxy:dead:{id}`         đang bị loại (có TTL, `proxies check` gỡ khi proxy sống lại);
- `proxy:cool:{id}:{domain}` đang nghỉ với domain đó sau 403/429;
- `proxy:sticky:{domain}`   proxy đang gắn với domain.

Redis chỉ giữ `id` (hash của URL), không giữ URL/mật khẩu proxy: mỗi process tự đọc file.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import SplitResult, quote, unquote, urlsplit

from jobcrawler.logging import get_logger
from jobcrawler.net.kv import KeyValueStore

log = get_logger(__name__)

SCHEMES = frozenset({"http", "https", "socks4", "socks4a", "socks5", "socks5h"})
INITIAL_SCORE = 50
MAX_SCORE = 100


class ProxyParseError(ValueError):
    pass


class NoProxyAvailableError(RuntimeError):
    """Có cấu hình proxy nhưng tất cả đang nghỉ hoặc đã bị loại."""


@dataclass(frozen=True, slots=True)
class Proxy:
    id: str
    url: str

    @property
    def _parts(self) -> SplitResult:
        return urlsplit(self.url)

    @property
    def scheme(self) -> str:
        return self._parts.scheme

    @property
    def display(self) -> str:
        """URL đã che mật khẩu, dùng để in/log."""
        p = self._parts
        user = f"{unquote(p.username)}:***@" if p.username else ""
        return f"{p.scheme}://{user}{p.hostname}:{p.port}"

    @property
    def browser_compatible(self) -> bool:
        # Chromium không hỗ trợ SOCKS có xác thực.
        return self.scheme in {"http", "https"} or not self._parts.username

    def playwright(self) -> dict[str, str]:
        p = self._parts
        settings = {"server": f"{p.scheme}://{p.hostname}:{p.port}"}
        if p.username:
            settings["username"] = unquote(p.username)
            settings["password"] = unquote(p.password or "")
        return settings


def _proxy_id(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:10]


def parse_proxy_line(line: str) -> Proxy | None:
    """Một dòng của proxies.txt → `Proxy`. Dòng trống hoặc `#` trả None.

    Nhận: `scheme://[user:pass@]host:port`, `host:port`, `host:port:user:pass` (mặc định http).
    """
    line = line.split("#", 1)[0].strip()
    if not line:
        return None
    if "://" not in line:
        parts = line.split(":")
        if len(parts) == 2:
            line = f"http://{parts[0]}:{parts[1]}"
        elif len(parts) == 4:
            host, port, user, password = parts
            line = f"http://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"
        else:
            raise ProxyParseError(f"không hiểu định dạng proxy: {line!r}")
    parts = urlsplit(line)
    if parts.scheme not in SCHEMES:
        raise ProxyParseError(f"scheme proxy không hỗ trợ: {parts.scheme!r}")
    try:
        port = parts.port
    except ValueError as exc:
        raise ProxyParseError(f"port không hợp lệ: {line!r}") from exc
    if not parts.hostname or port is None:
        raise ProxyParseError(f"thiếu host hoặc port: {line!r}")
    url = f"{parts.scheme}://{parts.netloc}"
    return Proxy(id=_proxy_id(url), url=url)


def load_proxies(path: Path) -> list[Proxy]:
    """Đọc file proxy; file không tồn tại → danh sách rỗng (đi thẳng, có cảnh báo)."""
    if not path.exists():
        return []
    proxies: dict[str, Proxy] = {}
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        try:
            proxy = parse_proxy_line(line)
        except ProxyParseError as exc:
            log.warning("proxy.invalid_line", file=str(path), line=lineno, error=str(exc))
            continue
        if proxy is not None:
            proxies.setdefault(proxy.id, proxy)
    return list(proxies.values())


@dataclass(frozen=True, slots=True)
class ProxyState:
    proxy: Proxy
    score: int
    fails: int
    dead: bool


class ProxyPool:
    def __init__(
        self,
        kv: KeyValueStore,
        proxies: Sequence[Proxy],
        *,
        sticky_seconds: float = 1800,
        cooldown_seconds: float = 300,
        max_failures: int = 5,
        dead_seconds: float = 3600,
        rng: random.Random | None = None,
    ) -> None:
        self._kv = kv
        self.proxies = list(proxies)
        self._by_id = {p.id: p for p in self.proxies}
        self._sticky_seconds = sticky_seconds
        self._cooldown_seconds = cooldown_seconds
        self._max_failures = max_failures
        self._dead_seconds = dead_seconds
        self._rng = rng or random.Random()

    @property
    def enabled(self) -> bool:
        return bool(self.proxies)

    async def _scores(self, proxies: Sequence[Proxy]) -> list[int]:
        raw = await self._kv.mget([f"proxy:score:{p.id}" for p in proxies])
        return [INITIAL_SCORE if v is None else int(v) for v in raw]

    async def _available(self, proxies: Sequence[Proxy], domain: str) -> list[tuple[Proxy, int]]:
        if not proxies:
            return []
        dead = await self._kv.mget([f"proxy:dead:{p.id}" for p in proxies])
        cool = await self._kv.mget([f"proxy:cool:{p.id}:{domain}" for p in proxies])
        scores = await self._scores(proxies)
        return [
            (p, s)
            for p, d, c, s in zip(proxies, dead, cool, scores, strict=True)
            if d is None and c is None
        ]

    async def acquire(self, domain: str, *, browser: bool = False) -> Proxy | None:
        """Proxy cho `domain`; None nếu không cấu hình proxy (đi thẳng)."""
        if not self.proxies:
            return None
        candidates = [p for p in self.proxies if p.browser_compatible] if browser else self.proxies
        sticky_key = f"proxy:sticky:{domain}"
        available = dict(await self._available(candidates, domain))
        sticky_id = await self._kv.get(sticky_key)
        if sticky_id is not None:
            sticky = self._by_id.get(sticky_id.decode())
            if sticky is not None and sticky in available:
                return sticky
        if not available:
            raise NoProxyAvailableError(f"không còn proxy dùng được cho {domain}")
        items = list(available.items())
        choice = self._rng.choices([p for p, _ in items], weights=[s + 1 for _, s in items])[0]
        if not await self._kv.set_nx(sticky_key, choice.id, ttl=self._sticky_seconds):
            # Process khác vừa gắn proxy cho domain này: dùng proxy đó nếu còn tốt.
            winner_id = await self._kv.get(sticky_key)
            winner = self._by_id.get(winner_id.decode()) if winner_id else None
            if winner is not None and winner in available:
                return winner
            await self._kv.set(sticky_key, choice.id, ttl=self._sticky_seconds)
        return choice

    async def _adjust_score(self, proxy: Proxy, delta: int) -> None:
        key = f"proxy:score:{proxy.id}"
        if await self._kv.get(key) is None:
            await self._kv.set_nx(key, INITIAL_SCORE)
        score = await self._kv.incrby(key, delta)
        if score > MAX_SCORE or score < 0:
            await self._kv.set(key, min(MAX_SCORE, max(0, score)))

    async def _unstick(self, proxy: Proxy, domain: str) -> None:
        key = f"proxy:sticky:{domain}"
        current = await self._kv.get(key)
        if current is not None and current.decode() == proxy.id:
            await self._kv.delete(key)

    async def report_success(self, proxy: Proxy, domain: str) -> None:
        await self._kv.delete(f"proxy:fails:{proxy.id}")
        await self._adjust_score(proxy, +1)

    async def report_blocked(self, proxy: Proxy, domain: str, seconds: float | None = None) -> None:
        """403/429/challenge: cho proxy nghỉ với domain này, đổi proxy cho domain."""
        await self._kv.set(
            f"proxy:cool:{proxy.id}:{domain}", 1, ttl=seconds or self._cooldown_seconds
        )
        await self._unstick(proxy, domain)
        await self._adjust_score(proxy, -5)
        log.info("proxy.cooldown", proxy_id=proxy.id, domain=domain)

    async def report_failure(self, proxy: Proxy, domain: str) -> None:
        """Lỗi kết nối qua proxy. Lỗi liên tiếp đủ ngưỡng thì loại proxy."""
        fails = await self._kv.incrby(f"proxy:fails:{proxy.id}")
        await self._adjust_score(proxy, -10)
        await self._unstick(proxy, domain)
        if fails >= self._max_failures:
            await self.mark_dead(proxy, reason=f"{fails} lỗi liên tiếp")

    async def mark_dead(self, proxy: Proxy, *, reason: str) -> None:
        await self._kv.set(f"proxy:dead:{proxy.id}", reason, ttl=self._dead_seconds)
        log.warning("proxy.dead", proxy_id=proxy.id, reason=reason)

    async def mark_healthy(self, proxy: Proxy) -> None:
        await self._kv.delete(f"proxy:dead:{proxy.id}", f"proxy:fails:{proxy.id}")
        key = f"proxy:score:{proxy.id}"
        raw = await self._kv.get(key)
        if raw is None or int(raw) < INITIAL_SCORE:
            await self._kv.set(key, INITIAL_SCORE)

    async def states(self) -> list[ProxyState]:
        ids = [p.id for p in self.proxies]
        scores = await self._scores(self.proxies)
        fails = await self._kv.mget([f"proxy:fails:{i}" for i in ids])
        dead = await self._kv.mget([f"proxy:dead:{i}" for i in ids])
        return [
            ProxyState(proxy=p, score=s, fails=int(f or 0), dead=d is not None)
            for p, s, f, d in zip(self.proxies, scores, fails, dead, strict=True)
        ]
