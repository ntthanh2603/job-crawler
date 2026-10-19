"""CLI `jobworker`: `run` (vòng lặp chính), `check` (gateway + proxy tại worker), `version`."""

from __future__ import annotations

import asyncio
import sys
import time
import warnings
from typing import TYPE_CHECKING, Annotated

import typer

from jobworker import __version__

if TYPE_CHECKING:
    from jobworker.config import WorkerSettings

app = typer.Typer(
    name="jobworker",
    help="Worker lấy trang của jobcrawler: nhận việc từ gateway (gRPC), tải qua proxy.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,
)

# Trả IP đi ra dạng `ip=1.2.3.4` (một dòng mỗi trường).
TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"

if sys.platform == "win32":
    # Playwright cần ProactorEventLoop; curl_cffi tự chạy selector ở thread phụ, đúng ý muốn.
    from curl_cffi.utils import CurlCffiWarning

    warnings.filterwarnings("ignore", message=r"\s*Proactor event loop", category=CurlCffiWarning)


def _settings() -> WorkerSettings:
    # Import muộn: `version` / `--help` không cần đọc cấu hình.
    from jobworker.config import WorkerSettings
    from jobworker.logging import configure_logging

    settings = WorkerSettings()
    configure_logging(settings.log_level, settings.log_format)
    return settings


@app.command()
def run() -> None:
    """Kết nối gateway và nhận việc tới khi có SIGTERM / Ctrl-C (hoặc gateway yêu cầu drain)."""
    from jobworker.client import run_worker

    asyncio.run(run_worker(_settings()))


@app.command()
def check(
    timeout: Annotated[
        float, typer.Option(help="Giây chờ kết nối gateway / mỗi proxy.", min=1)
    ] = 10.0,
) -> None:
    """Kiểm tra kết nối tới gateway và từng proxy tại worker (in IP đi ra)."""
    settings = _settings()
    ok = asyncio.run(_check(settings, timeout))
    if not ok:
        raise typer.Exit(1)


async def _check(settings: WorkerSettings, timeout: float) -> bool:
    import zstandard

    from jcproto.v1 import crawl_pb2
    from jobworker.client import open_channel
    from jobworker.fetch import HttpFetcher
    from jobworker.proxies import load_proxies

    channel = open_channel(settings)
    started = time.monotonic()
    try:
        await asyncio.wait_for(channel.channel_ready(), timeout=timeout)
        gateway_ok = True
        typer.secho(
            f"gateway {settings.gateway_addr}: OK ({(time.monotonic() - started) * 1000:.0f} ms)"
            + (", TLS" if settings.gateway_tls else ""),
            fg=typer.colors.GREEN,
        )
    except TimeoutError:
        gateway_ok = False
        typer.secho(
            f"gateway {settings.gateway_addr}: không kết nối được sau {timeout:.0f} s",
            fg=typer.colors.RED,
            err=True,
        )
    finally:
        await channel.close()

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
        url=TRACE_URL,
        timeout_seconds=timeout,
        allow_redirects=True,
    )

    async def probe(url: str | None, proxy_id: str) -> tuple[str, crawl_pb2.FetchResult]:
        return proxy_id, await fetcher.execute(task, proxy=url, proxy_id=proxy_id)

    try:
        results = await asyncio.gather(probe(None, ""), *(probe(p.url, p.id) for p in proxies))
    finally:
        await fetcher.close()

    labels = {p.id: f"{p.id} {p.display}" for p in proxies}
    for proxy_id, result in results:
        label = labels.get(proxy_id, "đi thẳng (không proxy)")
        if result.error_kind != crawl_pb2.ERROR_KIND_NONE or result.status != 200:
            reason = result.error or f"HTTP {result.status}"
            typer.secho(f"{label}: LỖI {reason}", fg=typer.colors.RED)
            continue
        body = result.body
        if result.body_zstd:
            body = zstandard.ZstdDecompressor().decompress(body)
        fields = dict(
            line.split("=", 1) for line in body.decode(errors="replace").splitlines() if "=" in line
        )
        typer.secho(
            f"{label}: IP {fields.get('ip', '?')} ({fields.get('loc', '?')}),"
            f" {result.duration_ms} ms",
            fg=typer.colors.GREEN,
        )
    if not proxies:
        typer.echo("Không có proxy tại worker (WORKER_PROXIES / WORKER_PROXIES_FILE).")
    return gateway_ok


@app.command()
def version() -> None:
    """In phiên bản jobworker."""
    typer.echo(__version__)


def main() -> None:
    # Console/pipe trên Windows có thể không phải UTF-8; thông báo tiếng Việt không được vỡ.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None and (stream.encoding or "").lower() != "utf-8":
            reconfigure(encoding="utf-8", errors="replace")
    app()


if __name__ == "__main__":
    main()
