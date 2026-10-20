"""Proxy tại worker: pool trong bộ nhớ của một process, xoay vòng dần.

Danh sách: proxy riêng của máy (WORKER_PROXIES / WORKER_PROXIES_FILE), không có thì danh sách
proxy chung gateway gửi xuống (`WorkerConfig.shared_proxies`, PROXY_MODE=worker; đổi danh sách thì
`replace`).

- Mỗi domain gắn một proxy (sticky) `sticky_seconds` giây; cần proxy mới thì chọn lần lượt
  (round-robin) trong các proxy còn dùng được với domain đó. Mỗi worker bắt đầu vòng ở một vị trí
  riêng (`start`) để các worker không cùng dồn vào proxy đầu danh sách.
- 403/407/429 → proxy nghỉ với domain đó `cooldown_seconds`. Lỗi kết nối qua proxy → cũng nghỉ với
  domain đó và tính một lỗi liên tiếp; đủ `max_failures` lỗi liên tiếp thì loại `dead_seconds` giây.
- id: proxy riêng `w:<hash>` (backend phân biệt với proxy chung); proxy chung dùng đúng id của
  backend (`<hash>`, cùng cách tính) để `source_fetches` khớp bảng proxy ở trang Cài đặt. Không log
  mật khẩu: in/log bằng `Proxy.display` / `mask()`.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from jobworker.logging import get_logger

log = get_logger(__name__)

SCHEMES = frozenset({"http", "https", "socks4", "socks4a", "socks5", "socks5h"})
SWEEP_EVERY = 1000  # dọn sticky/cooldown hết hạn sau chừng này lần chọn


class ProxyParseError(ValueError):
    pass


def mask(url: str) -> str:
    """URL proxy đã che mật khẩu, dùng để in/log."""
    try:
        p = urlsplit(url)
        user = f"{unquote(p.username)}:***@" if p.username else ""
        return f"{p.scheme}://{user}{p.hostname}:{p.port}"
    except ValueError:
        return "<proxy không hợp lệ>"


def playwright_settings(url: str) -> dict[str, str]:
    """URL proxy → tham số `proxy` của Playwright (tách user/pass ra khỏi server)."""
    p = urlsplit(url)
    settings = {"server": f"{p.scheme}://{p.hostname}:{p.port}"}
    if p.username:
        settings["username"] = unquote(p.username)
        settings["password"] = unquote(p.password or "")
    return settings


def browser_compatible(url: str) -> bool:
    """Chromium không dùng được SOCKS có xác thực."""
    p = urlsplit(url)
    return p.scheme in {"http", "https"} or not p.username


def proxy_id(url: str) -> str:
    return "w:" + hashlib.sha1(url.encode()).hexdigest()[:10]


@dataclass(frozen=True, slots=True)
class Proxy:
    id: str
    url: str

    @property
    def display(self) -> str:
        return mask(self.url)

    @property
    def browser_compatible(self) -> bool:
        return browser_compatible(self.url)

    def playwright(self) -> dict[str, str]:
        return playwright_settings(self.url)


def parse_proxy_line(line: str) -> Proxy | None:
    """Một dòng proxy → `Proxy`. Dòng trống hoặc `#` trả None.

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
            raise ProxyParseError(f"không hiểu định dạng proxy: {mask(line)!r}")
    parts = urlsplit(line)
    if parts.scheme not in SCHEMES:
        raise ProxyParseError(f"scheme proxy không hỗ trợ: {parts.scheme!r}")
    try:
        port = parts.port
    except ValueError as exc:
        raise ProxyParseError(f"port không hợp lệ: {mask(line)!r}") from exc
    if not parts.hostname or port is None:
        raise ProxyParseError(f"thiếu host hoặc port: {mask(line)!r}")
    url = f"{parts.scheme}://{parts.netloc}"
    return Proxy(id=proxy_id(url), url=url)


def shared_proxies(urls: Iterable[str]) -> list[Proxy]:
    """Danh sách proxy chung gateway gửi xuống (URL đã chuẩn hoá ở backend), id như backend."""
    proxies: dict[str, Proxy] = {}
    for url in urls:
        try:
            proxy = parse_proxy_line(url)
        except ProxyParseError as exc:
            log.warning("worker.shared_proxy_invalid", error=str(exc))
            continue
        if proxy is not None:
            shared = Proxy(id=hashlib.sha1(proxy.url.encode()).hexdigest()[:10], url=proxy.url)
            proxies.setdefault(shared.id, shared)
    return list(proxies.values())


def start_offset(worker_id: str) -> int:
    """Vị trí bắt đầu vòng xoay, cố định theo worker (không phụ thuộc PYTHONHASHSEED)."""
    return int.from_bytes(hashlib.sha1(worker_id.encode()).digest()[:4], "big")


def load_proxies(urls: Iterable[str] = (), path: Path | None = None) -> list[Proxy]:
    """Proxy từ danh sách (WORKER_PROXIES) và file (WORKER_PROXIES_FILE), bỏ trùng.

    Dòng sai định dạng bị bỏ qua (có cảnh báo); file không tồn tại → cảnh báo."""
    lines: list[tuple[str, int, str]] = [("WORKER_PROXIES", i, u) for i, u in enumerate(urls, 1)]
    if path is not None:
        if path.exists():
            text = path.read_text(encoding="utf-8")
            lines += [(str(path), i, line) for i, line in enumerate(text.splitlines(), 1)]
        else:
            log.warning("worker.proxies_file_missing", file=str(path))
    proxies: dict[str, Proxy] = {}
    for origin, lineno, line in lines:
        try:
            proxy = parse_proxy_line(line)
        except ProxyParseError as exc:
            log.warning("worker.proxy_invalid", source=origin, line=lineno, error=str(exc))
            continue
        if proxy is not None:
            proxies.setdefault(proxy.id, proxy)
    return list(proxies.values())


@dataclass(slots=True)
class _Health:
    fails: int = 0  # lỗi kết nối liên tiếp
    dead_until: float = 0.0


class LocalProxyPool:
    """Pool proxy của một worker. Không có I/O: mọi thao tác đồng bộ, gọi từ event loop."""

    def __init__(
        self,
        proxies: Iterable[Proxy],
        *,
        sticky_seconds: float = 600,
        cooldown_seconds: float = 120,
        max_failures: int = 5,
        dead_seconds: float = 600,
        start: int = 0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.proxies = list(proxies)
        self._by_id = {p.id: p for p in self.proxies}
        self._health = {p.id: _Health() for p in self.proxies}
        self._sticky_seconds = sticky_seconds
        self._cooldown_seconds = cooldown_seconds
        self._max_failures = max_failures
        self._dead_seconds = dead_seconds
        self._clock = clock
        self._sticky: dict[str, tuple[str, float]] = {}  # host → (proxy id, hết hạn)
        self._cool: dict[tuple[str, str], float] = {}  # (proxy id, host) → hết nghỉ lúc
        self._start = start
        self._next = start % len(self.proxies) if self.proxies else 0  # vị trí round-robin
        self._picks = 0

    def __len__(self) -> int:
        return len(self.proxies)

    def replace(self, proxies: Iterable[Proxy]) -> None:
        """Đổi danh sách (gateway gửi danh sách chung mới): proxy còn trong danh sách giữ sức khoẻ,
        thời gian nghỉ và domain đang gắn; proxy bị bỏ thì quên hẳn."""
        self.proxies = list(proxies)
        self._by_id = {p.id: p for p in self.proxies}
        self._health = {p.id: self._health.get(p.id) or _Health() for p in self.proxies}
        self._sticky = {h: s for h, s in self._sticky.items() if s[0] in self._by_id}
        self._cool = {k: until for k, until in self._cool.items() if k[0] in self._by_id}
        self._next = self._start % len(self.proxies) if self.proxies else 0

    def _dead(self, proxy_id: str, now: float) -> bool:
        health = self._health[proxy_id]
        if health.dead_until and health.dead_until <= now:
            # Hết hạn loại: cho dùng lại, đếm lỗi từ đầu.
            health.dead_until = 0.0
            health.fails = 0
        return health.dead_until > now

    def _usable(self, proxy: Proxy, host: str, now: float) -> bool:
        if self._dead(proxy.id, now):
            return False
        return self._cool.get((proxy.id, host), 0.0) <= now

    def alive(self) -> int:
        """Số proxy chưa bị loại."""
        now = self._clock()
        return sum(not self._dead(p.id, now) for p in self.proxies)

    def choose(self, host: str, *, browser: bool = False) -> Proxy | None:
        """Proxy cho `host`: proxy đang gắn nếu còn dùng được, không thì proxy kế tiếp theo vòng.
        None khi không có proxy nào dùng được (caller đi thẳng)."""
        if not self.proxies:
            return None
        now = self._clock()
        self._picks += 1
        if self._picks % SWEEP_EVERY == 0:
            self._sweep(now)
        sticky = self._sticky.get(host)
        if sticky is not None and sticky[1] > now:
            proxy = self._by_id[sticky[0]]
            if self._usable(proxy, host, now) and (not browser or proxy.browser_compatible):
                return proxy
        n = len(self.proxies)
        for i in range(n):
            proxy = self.proxies[(self._next + i) % n]
            if browser and not proxy.browser_compatible:
                continue
            if self._usable(proxy, host, now):
                self._next = (self._next + i + 1) % n
                self._sticky[host] = (proxy.id, now + self._sticky_seconds)
                return proxy
        return None

    def _unstick(self, proxy: Proxy, host: str) -> None:
        sticky = self._sticky.get(host)
        if sticky is not None and sticky[0] == proxy.id:
            del self._sticky[host]

    def report_success(self, proxy: Proxy, host: str) -> None:
        if (health := self._health.get(proxy.id)) is not None:
            health.fails = 0

    def report_blocked(self, proxy: Proxy, host: str) -> None:
        """403/407/429: proxy nghỉ với domain này, domain đổi sang proxy khác."""
        if proxy.id not in self._health:
            return
        self._cool[(proxy.id, host)] = self._clock() + self._cooldown_seconds
        self._unstick(proxy, host)
        log.info("worker.proxy_cooldown", proxy_id=proxy.id, host=host)

    def report_failure(self, proxy: Proxy, host: str) -> None:
        """Lỗi kết nối qua proxy: nghỉ với domain này; lỗi liên tiếp đủ ngưỡng thì loại proxy."""
        health = self._health.get(proxy.id)
        if health is None:
            return
        now = self._clock()
        health.fails += 1
        self._cool[(proxy.id, host)] = now + self._cooldown_seconds
        self._unstick(proxy, host)
        if health.fails >= self._max_failures and health.dead_until <= now:
            health.dead_until = now + self._dead_seconds
            log.warning(
                "worker.proxy_dead",
                proxy_id=proxy.id,
                proxy=proxy.display,
                fails=health.fails,
                seconds=self._dead_seconds,
            )

    def _sweep(self, now: float) -> None:
        self._sticky = {h: s for h, s in self._sticky.items() if s[1] > now}
        self._cool = {k: until for k, until in self._cool.items() if until > now}
