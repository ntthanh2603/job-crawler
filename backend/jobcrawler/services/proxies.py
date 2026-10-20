"""Danh sách proxy chung cho trang Cài đặt → Proxy: trạng thái xoay vòng của từng proxy và kiểm
tra qua worker.

Danh sách = cài đặt `proxies` (lưu trong DB, sửa ở trang Cài đặt) rồi `PROXIES_FILE`, bỏ trùng —
đúng thứ `resources.central_proxies` nạp vào pool khi PROXY_MODE=central. Trạng thái đọc từ các
khoá pool trên Redis chính (`net.proxy_pool`): điểm, lỗi liên tiếp, bị loại (lý do, còn bao lâu),
số domain đang cho nghỉ (sau 403/429) và số domain đang gắn (sticky). Mỗi proxy nhận diện bằng `id`
(hash của URL) — không trả URL / mật khẩu; `line` là dòng đã che mật khẩu, khớp giá trị cài đặt
`proxies` mà trang Cài đặt nhận.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from redis.asyncio import Redis

from jobcrawler.config import Settings
from jobcrawler.net.kv import RedisKV
from jobcrawler.net.proxy_pool import Proxy, ProxyParseError, load_proxies, parse_proxy_line
from jobcrawler.resources import Resources, make_proxy_pool
from jobcrawler.services import health
from jobcrawler.services.app_settings import mask_proxy

# Số proxy tối đa mỗi lần kiểm tra (giao diện chia danh sách dài thành nhiều lần).
CHECK_BATCH = 20
CHECK_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True, slots=True)
class ProxyStatus:
    id: str
    line: str  # dòng đã che mật khẩu (như giá trị cài đặt `proxies` trả ra API)
    source: str  # "settings" (trang Cài đặt) | "file" (PROXIES_FILE)
    scheme: str
    host: str
    port: int
    username: str | None
    has_password: bool
    score: int  # 0..100, cao thì hay được chọn hơn
    fails: int  # lỗi kết nối liên tiếp
    dead: bool  # đang bị loại khỏi vòng xoay
    dead_reason: str | None
    dead_seconds: int | None  # còn bao lâu thì được thử lại
    cooling_domains: int  # số domain đang cho proxy nghỉ (sau 403 / 429)
    sticky_domains: int  # số domain đang gắn với proxy này


@dataclass(frozen=True, slots=True)
class Listed:
    proxy: Proxy
    line: str
    source: str


def listed(settings: Settings) -> list[Listed]:
    """Proxy chung theo thứ tự: cài đặt `proxies` rồi PROXIES_FILE, bỏ trùng và dòng lỗi."""
    out: list[Listed] = []
    seen: set[str] = set()
    for line in settings.proxies:
        try:
            proxy = parse_proxy_line(line)
        except ProxyParseError:
            continue
        if proxy is not None and proxy.id not in seen:
            seen.add(proxy.id)
            out.append(Listed(proxy, mask_proxy(line), "settings"))
    for proxy in load_proxies(settings.proxies_file):
        if proxy.id not in seen:
            seen.add(proxy.id)
            out.append(Listed(proxy, mask_proxy(proxy.url), "file"))
    return out


async def _count_ids(redis: Redis, pattern: str, *, prefix_len: int) -> Counter[str]:
    """Đếm theo id proxy các khoá `<...>:proxy:cool:<id>:<domain>`."""
    counts: Counter[str] = Counter()
    async for key in redis.scan_iter(match=pattern, count=1000):
        text = key.decode() if isinstance(key, bytes) else str(key)
        counts[text[prefix_len:].split(":", 1)[0]] += 1
    return counts


async def _sticky_ids(redis: Redis, pattern: str) -> Counter[str]:
    """Đếm theo id proxy giá trị các khoá `<...>:proxy:sticky:<domain>`."""
    counts: Counter[str] = Counter()
    batch: list[bytes] = []

    async def flush() -> None:
        for value in await redis.mget(batch):
            if value is not None:
                counts[value.decode() if isinstance(value, bytes) else str(value)] += 1
        batch.clear()

    async for key in redis.scan_iter(match=pattern, count=1000):
        batch.append(key)
        if len(batch) >= 500:
            await flush()
    if batch:
        await flush()
    return counts


async def statuses(redis: Redis, settings: Settings) -> list[ProxyStatus]:
    items = listed(settings)
    if not items:
        return []
    prefix = settings.redis_prefix
    pool = make_proxy_pool(settings, RedisKV(redis, prefix=prefix), [i.proxy for i in items])
    states = await pool.states()
    dead_keys = [f"{prefix}:proxy:dead:{i.proxy.id}" for i in items]
    reasons = await redis.mget(dead_keys)
    pipe = redis.pipeline()
    for key in dead_keys:
        pipe.ttl(key)
    ttls = await pipe.execute()
    cool_prefix = f"{prefix}:proxy:cool:"
    cooling = await _count_ids(redis, f"{cool_prefix}*", prefix_len=len(cool_prefix))
    sticky = await _sticky_ids(redis, f"{prefix}:proxy:sticky:*")
    out: list[ProxyStatus] = []
    for item, state, reason, ttl in zip(items, states, reasons, ttls, strict=True):
        parts = urlsplit(item.proxy.url)
        out.append(
            ProxyStatus(
                id=item.proxy.id,
                line=item.line,
                source=item.source,
                scheme=parts.scheme,
                host=parts.hostname or "",
                port=parts.port or 0,
                username=unquote(parts.username) if parts.username else None,
                has_password=bool(parts.password),
                score=state.score,
                fails=state.fails,
                dead=state.dead,
                dead_reason=reason.decode() if isinstance(reason, bytes) else reason,
                dead_seconds=int(ttl) if state.dead and ttl and ttl > 0 else None,
                cooling_domains=cooling[item.proxy.id],
                sticky_domains=sticky[item.proxy.id],
            )
        )
    return out


async def check(
    res: Resources,
    settings: Settings,
    ids: Sequence[str],
    *,
    timeout: float = CHECK_TIMEOUT_SECONDS,
) -> list[health.ProxyCheck]:
    """Gọi trang kiểm tra (PROXY_CHECK_URL) qua từng proxy trong `ids` từ worker (đúng đường
    request thật đi). Proxy sống được đưa lại vào vòng xoay, proxy lỗi bị loại. id lạ bỏ qua."""
    wanted = set(ids)
    proxies = [i.proxy for i in listed(settings) if i.proxy.id in wanted]
    if not proxies:
        return []
    pool = make_proxy_pool(settings, RedisKV(res.redis, prefix=settings.redis_prefix), proxies)
    return await health.check_proxies(
        res,
        pool=pool,
        url=settings.proxy_check_url,
        concurrency=len(proxies),
        timeout=timeout,
    )
