"""`jobworker check`: kết nối tới gateway và IP đi ra của worker — đi thẳng và qua từng proxy tại
worker (WORKER_PROXIES / WORKER_PROXIES_FILE), mỗi đường một request tới trang trace của Cloudflare.
CLI in kết quả (`jobworker.bootstrap.cli`)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import grpc
import zstandard

from jcproto.v1 import crawl_pb2
from jobworker.common.config import WorkerSettings
from jobworker.modules.fetch.http_service import HttpFetcher
from jobworker.services.channel import open_channel
from jobworker.services.proxies import load_proxies

# Trả IP đi ra dạng `ip=1.2.3.4` (một dòng mỗi trường).
TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"
DIRECT_LABEL = "đi thẳng (không proxy)"
READY_POLL_SECONDS = 0.02  # nhịp hỏi trạng thái kênh gRPC trong `check_gateway`


@dataclass(frozen=True, slots=True)
class GatewayCheck:
    ok: bool
    ms: float  # tới khi kênh sẵn sàng


@dataclass(frozen=True, slots=True)
class RouteCheck:
    proxy_id: str  # "" = đi thẳng
    label: str  # proxy đã che mật khẩu
    error: str  # "" = OK
    ip: str = ""
    loc: str = ""
    ms: int = 0


async def check_gateway(settings: WorkerSettings, timeout: float) -> GatewayCheck:
    """Mở kênh gRPC tới gateway, chờ sẵn sàng tối đa `timeout` giây.

    Hỏi trạng thái kênh mỗi READY_POLL_SECONDS, không dùng `channel_ready()`: huỷ nó khi hết hạn bỏ
    lại lệnh theo dõi kết nối trong gRPC core, lệnh đó xong sau khi event loop đã đóng thì thread
    poller của grpc.aio (ProactorEventLoop trên Windows) chết vì `Event loop is closed` và mọi lời
    gọi gRPC sau đó trong process treo."""
    channel = open_channel(settings)
    started = time.monotonic()
    try:
        while channel.get_state(try_to_connect=True) != grpc.ChannelConnectivity.READY:
            if time.monotonic() - started >= timeout:
                return GatewayCheck(ok=False, ms=(time.monotonic() - started) * 1000)
            await asyncio.sleep(READY_POLL_SECONDS)
        return GatewayCheck(ok=True, ms=(time.monotonic() - started) * 1000)
    finally:
        await channel.close()


def _trace_fields(result: crawl_pb2.FetchResult) -> dict[str, str]:
    body = result.body
    if result.body_zstd:
        body = zstandard.ZstdDecompressor().decompress(body)
    lines = body.decode(errors="replace").splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


async def check_routes(
    settings: WorkerSettings, timeout: float, *, url: str = TRACE_URL
) -> list[RouteCheck]:
    """Đi thẳng rồi từng proxy tại worker, song song: IP / vị trí đi ra hoặc lỗi."""
    proxies = load_proxies(settings.worker_proxies, settings.worker_proxies_file)
    fetcher = HttpFetcher(
        worker_id=settings.worker_id,
        impersonate=settings.worker_impersonate,
        max_clients=max(1, min(len(proxies) + 1, 32)),
    )
    task = crawl_pb2.FetchTask(
        task_id="check",
        kind=crawl_pb2.TASK_KIND_HTTP,
        method="GET",
        url=url,
        timeout_seconds=timeout,
        allow_redirects=True,
    )

    async def probe(proxy_url: str | None, proxy_id: str) -> tuple[str, crawl_pb2.FetchResult]:
        return proxy_id, await fetcher.execute(task, proxy=proxy_url, proxy_id=proxy_id)

    try:
        results = await asyncio.gather(probe(None, ""), *(probe(p.url, p.id) for p in proxies))
    finally:
        await fetcher.close()

    labels = {p.id: f"{p.id} {p.display}" for p in proxies}
    checks: list[RouteCheck] = []
    for proxy_id, result in results:
        label = labels.get(proxy_id, DIRECT_LABEL)
        if result.error_kind != crawl_pb2.ERROR_KIND_NONE or result.status != 200:
            error = result.error or f"HTTP {result.status}"
            checks.append(RouteCheck(proxy_id=proxy_id, label=label, error=error))
            continue
        fields = _trace_fields(result)
        checks.append(
            RouteCheck(
                proxy_id=proxy_id,
                label=label,
                error="",
                ip=fields.get("ip", "?"),
                loc=fields.get("loc", "?"),
                ms=result.duration_ms,
            )
        )
    return checks
