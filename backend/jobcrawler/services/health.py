"""Kiểm tra hạ tầng: proxy, LLM, và fetch thử một URL (debug)."""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import dataclass

from jobcrawler.common.logging import get_logger
from jobcrawler.modules.gateway.dispatch import DispatchError
from jobcrawler.prompts import healthcheck
from jobcrawler.services.llm import LLMResult
from jobcrawler.services.net.http import FetchResult, TransportError, decode_body
from jobcrawler.services.net.kv import RedisKV
from jobcrawler.services.net.proxy_pool import Proxy, ProxyPool
from jobcrawler.services.resources import Resources, central_proxies, make_proxy_pool

log = get_logger(__name__)


@dataclass(slots=True)
class ProxyCheck:
    proxy_id: str
    proxy: str  # đã che mật khẩu
    ok: bool
    status: int | None = None
    latency_ms: int | None = None
    exit_ip: str | None = None
    location: str | None = None
    error: str | None = None


def parse_exit_info(body: str) -> tuple[str | None, str | None]:
    """IP ra và quốc gia từ `cdn-cgi/trace` (key=value) hoặc JSON có `ip`/`country`."""
    body = body.strip()
    if body.startswith("{"):
        try:
            data = json.loads(body)
        except ValueError:
            return None, None
        return data.get("ip"), data.get("country") or data.get("loc")
    fields = dict(line.split("=", 1) for line in body.splitlines() if "=" in line)
    return fields.get("ip"), fields.get("loc")


def proxies_to_check(res: Resources) -> ProxyPool:
    """Danh sách proxy chung (PROXIES + PROXIES_FILE) bất kể PROXY_MODE: pool đang dùng khi
    PROXY_MODE=central, không thì dựng pool từ danh sách để vẫn kiểm tra được."""
    if res.proxies.enabled:
        return res.proxies
    kv = RedisKV(res.redis, prefix=res.settings.redis_prefix)  # trạng thái proxy: Redis chính
    return make_proxy_pool(res.settings, kv, central_proxies(res.settings))


async def check_proxies(
    res: Resources,
    *,
    pool: ProxyPool | None = None,
    url: str | None = None,
    concurrency: int = 20,
    timeout: float = 15,
) -> list[ProxyCheck]:
    """Gọi `url` qua từng proxy; proxy sống được gỡ trạng thái `dead`, proxy lỗi bị loại.

    Đi thẳng transport (bỏ qua rate limit/robots) vì mỗi proxy chỉ gọi một lần tới URL kiểm tra.
    Transport của `res.http` gửi việc tới worker lấy trang kèm proxy chỉ định: kiểm tra từ mạng
    của worker, đúng đường request thật đi. Không có worker / worker không trả lời thì không
    đánh giá được proxy (không loại).
    """
    url = url or res.settings.proxy_check_url
    pool = pool or res.proxies
    transport = res.http.transport
    sem = asyncio.Semaphore(concurrency)

    async def one(proxy: Proxy) -> ProxyCheck:
        async with sem:
            t0 = time.monotonic()
            try:
                raw = await transport.request(
                    "GET",
                    url,
                    headers={},
                    proxy=proxy.url,
                    timeout=timeout,
                    allow_redirects=True,
                )
            except DispatchError as exc:
                return ProxyCheck(proxy.id, proxy.display, ok=False, error=f"{exc.kind}: {exc}")
            except TransportError as exc:
                await pool.mark_dead(proxy, reason=f"check: {exc.kind}")
                return ProxyCheck(proxy.id, proxy.display, ok=False, error=f"{exc.kind}: {exc}")
            latency = int((time.monotonic() - t0) * 1000)
            if not 200 <= raw.status < 300:
                await pool.mark_dead(proxy, reason=f"check: http_{raw.status}")
                return ProxyCheck(
                    proxy.id,
                    proxy.display,
                    ok=False,
                    status=raw.status,
                    latency_ms=latency,
                    error=f"http_{raw.status}",
                )
            await pool.mark_healthy(proxy)
            ip, loc = parse_exit_info(decode_body(raw.content, raw.headers.get("content-type")))
            return ProxyCheck(
                proxy.id,
                proxy.display,
                ok=True,
                status=raw.status,
                latency_ms=latency,
                exit_ip=ip,
                location=loc,
            )

    results = await asyncio.gather(*(one(p) for p in pool.proxies))
    ok = sum(r.ok for r in results)
    log.info("proxies.check", total=len(results), ok=ok, failed=len(results) - ok, url=url)
    return list(results)


async def check_llm(res: Resources) -> tuple[LLMResult[healthcheck.HealthcheckAnswer], bool]:
    """Gọi prompt healthcheck; trả (kết quả, đúng hay không)."""
    nonce = secrets.token_hex(4)
    result = await res.llm.structured(
        healthcheck.PROMPT,
        healthcheck.HealthcheckAnswer,
        nonce=nonce,
        sentence="Công ty đang tuyển kỹ sư phần mềm làm việc tại Hà Nội.",
    )
    answer = result.value
    correct = (
        answer is not None
        and answer.ok
        and answer.nonce == nonce
        and answer.language.lower().startswith("vi")
    )
    return result, correct


async def probe_url(
    res: Resources,
    url: str,
    *,
    render: bool = False,
    check_robots: bool = True,
    use_cache: bool = True,
) -> FetchResult:
    if render:
        return await res.http.render(url, source="probe", check_robots=check_robots)
    return await res.http.fetch(url, source="probe", check_robots=check_robots, use_cache=use_cache)
