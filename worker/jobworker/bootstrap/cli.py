"""CLI `jobworker`: `run` (vòng lặp chính, `bootstrap/run.py`), `check` (gateway + proxy tại
worker, `modules/health/probe_service.py`), `health` (healthcheck của Docker, đọc file trạng thái
`modules/health/status_service.py`), `version`."""

from __future__ import annotations

import asyncio
import sys
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from jobworker import __version__
from jobworker.modules.health.status_service import GATEWAY_GRACE_SECONDS

if TYPE_CHECKING:
    from jobworker.common.config import WorkerSettings

app = typer.Typer(
    name="jobworker",
    help="Worker lấy trang của jobcrawler: nhận việc từ gateway (gRPC), tải qua proxy.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,
)

if sys.platform == "win32":
    # Playwright cần ProactorEventLoop; curl_cffi tự chạy selector ở thread phụ, đúng ý muốn.
    from curl_cffi.utils import CurlCffiWarning

    warnings.filterwarnings("ignore", message=r"\s*Proactor event loop", category=CurlCffiWarning)


def _settings() -> WorkerSettings:
    # Import muộn: `version` / `--help` không cần đọc cấu hình.
    from jobworker.common.config import WorkerSettings
    from jobworker.common.logging import configure_logging

    settings = WorkerSettings()
    configure_logging(settings.log_level, settings.log_format)
    return settings


@app.command()
def run() -> None:
    """Kết nối gateway và nhận việc tới khi có SIGTERM / Ctrl-C (hoặc gateway yêu cầu drain).
    Treo quá WORKER_WATCHDOG_SECONDS thì thoát mã 70; gateway từ chối WORKER_ID / WORKER_POOL thì
    thoát mã 1."""
    from jobworker.bootstrap.run import run_worker
    from jobworker.modules.gateway.worker_service import RegisterRejectedError

    try:
        asyncio.run(run_worker(_settings()))
    except RegisterRejectedError as exc:
        typer.secho(
            f"gateway từ chối đăng ký (kiểm tra WORKER_ID / WORKER_POOL): {exc}",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1) from None


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
    from jobworker.modules.health.probe_service import check_gateway, check_routes

    gateway = await check_gateway(settings, timeout)
    if gateway.ok:
        typer.secho(
            f"gateway {settings.gateway_addr}: OK ({gateway.ms:.0f} ms)"
            + (", TLS" if settings.gateway_tls else ""),
            fg=typer.colors.GREEN,
        )
    else:
        typer.secho(
            f"gateway {settings.gateway_addr}: không kết nối được sau {timeout:.0f} s",
            fg=typer.colors.RED,
            err=True,
        )

    routes = await check_routes(settings, timeout)
    for route in routes:
        if route.error:
            typer.secho(f"{route.label}: LỖI {route.error}", fg=typer.colors.RED)
        else:
            typer.secho(
                f"{route.label}: IP {route.ip} ({route.loc}), {route.ms} ms",
                fg=typer.colors.GREEN,
            )
    if not any(route.proxy_id for route in routes):
        typer.echo("Không có proxy tại worker (WORKER_PROXIES / WORKER_PROXIES_FILE).")
    return gateway.ok


@app.command()
def health(
    max_age: Annotated[
        float | None,
        typer.Option(
            help="Giây; file trạng thái cũ hơn thì coi là treo."
            " Mặc định max(30, 3 × chu kỳ heartbeat ghi trong file).",
            min=1,
        ),
    ] = None,
    gateway_grace: Annotated[
        float,
        typer.Option(
            help="Giây không kết nối được gateway liên tục thì coi là không khoẻ; 0 = bỏ qua.",
            min=0,
        ),
    ] = GATEWAY_GRACE_SECONDS,
    file: Annotated[
        Path | None, typer.Option(help="File trạng thái; mặc định WORKER_HEALTH_FILE.")
    ] = None,
) -> None:
    """Healthcheck (Docker): mã 0 khi worker còn chạy và không mất gateway quá lâu, 1 khi không."""
    from jobworker.modules.health.status_service import check as check_health

    if file is None:
        from jobworker.common.config import WorkerSettings

        file = WorkerSettings().worker_health_file
    ok, reason = check_health(file, max_age=max_age, gateway_grace=gateway_grace)
    if not ok:
        typer.echo(f"unhealthy: {reason}", err=True)
        raise typer.Exit(1)
    typer.echo(f"healthy: {reason}")


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
