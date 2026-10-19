"""CLI `jobcrawler`. Chỉ parse tham số, mở tài nguyên và gọi vào `jobcrawler.services`."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
import time
from collections.abc import Coroutine
from typing import Annotated, Any
from uuid import UUID

import typer

from jobcrawler.config import get_settings
from jobcrawler.logging import configure_logging
from jobcrawler.resources import ResourceUnavailableError, open_resources
from jobcrawler.schemas import CompanyIn, CompanyResult, JobOut, RunStatus, RunTrigger

app = typer.Typer(
    name="jobcrawler",
    help="Thu thập tin tuyển dụng cho danh sách doanh nghiệp.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,
)
db_app = typer.Typer(help="Schema Postgres (Alembic).", no_args_is_help=True)
proxies_app = typer.Typer(help="Proxy pool.", no_args_is_help=True)
llm_app = typer.Typer(help="LLM (vLLM + Qwen).", no_args_is_help=True)
queue_app = typer.Typer(help="Hàng đợi job (Redis Streams).", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(proxies_app, name="proxies")
app.add_typer(llm_app, name="llm")
app.add_typer(queue_app, name="queue")


@app.callback()
def _setup(
    no_proxy: Annotated[
        bool,
        typer.Option("--no-proxy", help="Không dùng proxy, đi thẳng (như PROXY_MODE=off)."),
    ] = False,
) -> None:
    if no_proxy:
        os.environ["PROXY_MODE"] = "off"
        get_settings.cache_clear()
    configure_logging(get_settings())


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    try:
        return asyncio.run(coro)
    except ResourceUnavailableError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        typer.secho("Gợi ý: docker compose up -d", err=True)
        raise typer.Exit(2) from None


# --- db --------------------------------------------------------------------------------------


@db_app.command("upgrade")
def db_upgrade(revision: Annotated[str, typer.Argument()] = "head") -> None:
    """Nâng schema lên `revision` (mặc định head)."""
    from jobcrawler.db import migrate

    migrate.upgrade(revision)


@db_app.command("downgrade")
def db_downgrade(revision: Annotated[str, typer.Argument()]) -> None:
    """Hạ schema về `revision` (ví dụ -1 hoặc base)."""
    from jobcrawler.db import migrate

    migrate.downgrade(revision)


@db_app.command("current")
def db_current() -> None:
    """In revision hiện tại của DB."""
    from jobcrawler.db import migrate

    migrate.current()


@db_app.command("maintain")
def db_maintain(
    no_prune: Annotated[bool, typer.Option("--no-prune", help="Không xoá dữ liệu cũ.")] = False,
    no_compress: Annotated[
        bool, typer.Option("--no-compress", help="Không huấn luyện từ điển / nén lại.")
    ] = False,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Bảo trì (như cron mỗi giờ): lượt chạy treo → failed, xoá dữ liệu cũ, huấn luyện từ điển
    nén và nén lại dữ liệu."""
    from jobcrawler.services import maintenance

    async def go() -> maintenance.MaintenanceReport:
        async with open_resources(db_logs=False) as res:
            return await maintenance.run_maintenance(
                res, prune=not no_prune, compression=not no_compress
            )

    report = _run(go())
    if json_out:
        typer.echo(json.dumps(report.as_dict(), ensure_ascii=False, indent=2, default=str))
        return
    if report.skipped:
        typer.secho(report.skipped, fg=typer.colors.YELLOW)
        return
    typer.echo(
        f"lượt chạy treo → failed: {report.abandoned_runs} doanh nghiệp,"
        f" {report.abandoned_board_runs} index"
    )
    if report.deleted:
        typer.echo("đã xoá: " + ", ".join(f"{t} {n}" for t, n in report.deleted.items()))
    for t in report.trained:
        state = f"từ điển mới {t.dict_id}" if t.adopted else t.reason
        ratio = f" (nén x{t.ratio_before} → x{t.ratio_after})" if t.ratio_after is not None else ""
        typer.echo(f"từ điển {t.kind}: {state}{ratio}")
    if report.recompressed:
        typer.echo("nén lại: " + ", ".join(f"{k} {n}" for k, n in report.recompressed.items()))
    typer.echo(f"{report.duration_ms / 1000:.1f} s")


@db_app.command("reclean")
def db_reclean(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Chỉ đếm, không ghi.")] = False,
) -> None:
    """Áp lại bộ làm sạch nội dung (che liên hệ, sửa định dạng) lên job đã lưu, không cần chạy
    lại doanh nghiệp."""
    from jobcrawler.services import maintenance

    async def go() -> maintenance.RecleanReport:
        async with open_resources(db_logs=False) as res:
            return await maintenance.reclean_jobs(res.sessionmaker, dry_run=dry_run)

    report = _run(go())
    verb = "cần sửa" if dry_run else "đã sửa"
    typer.echo(f"đã quét {report.scanned} job · {verb} {report.changed}")


@db_app.command("dedup")
def db_dedup() -> None:
    """Điền dấu vân tay cho job cũ và gộp các job đang mở trùng nội dung (job cũ nhất giữ lại)."""
    from jobcrawler.services import maintenance

    async def go() -> maintenance.DedupReport:
        async with open_resources(db_logs=False) as res:
            return await maintenance.dedupe_jobs(res.sessionmaker)

    r = _run(go())
    typer.echo(
        f"đã quét {r.scanned} job · điền dấu vân tay {r.fingerprinted} · gộp {r.merged} job trùng"
    )


@db_app.command("revision")
def db_revision(
    message: Annotated[str, typer.Option("-m", "--message")],
    autogenerate: Annotated[bool, typer.Option(help="So model với DB để sinh migration.")] = True,
) -> None:
    """Tạo migration mới (dev)."""
    from jobcrawler.db import migrate

    migrate.revision(message, autogenerate=autogenerate)


# --- chạy ------------------------------------------------------------------------------------


async def _run_company(company_in: CompanyIn) -> CompanyResult:
    from jobcrawler.services import companies

    async with open_resources() as res:
        return await companies.process_company(res, company_in)


def _salary_text(job: JobOut) -> str:
    s = job.salary
    if s is None:
        return "-"
    if s.min is None and s.max is None:
        return "thoả thuận" if s.negotiable else (s.raw or "-")

    def amount(v: float | None) -> str:
        if v is None:
            return "?"
        if s.currency == "VND" and v >= 1_000_000:
            return f"{v / 1_000_000:g} tr"
        return f"{v:,.0f}"

    span = amount(s.min) if s.min == s.max else f"{amount(s.min)}-{amount(s.max)}"
    period = f"/{s.period.value}" if s.period else ""
    return f"{span} {s.currency or ''}{period}".strip()


def _job_line(job: JobOut) -> str:
    places = ", ".join(dict.fromkeys(loc.province for loc in job.locations if loc.province)) or "-"
    tags = "/".join(
        v.value for v in (job.level, job.employment_type, job.work_mode) if v.value != "unknown"
    )
    sources = f" +{len(job.source_urls) - 1} nguồn" if len(job.source_urls) > 1 else ""
    return (
        f"  {job.title} — {places} — {_salary_text(job)}"
        + (f" [{tags}]" if tags else "")
        + f" · {job.canonical_source}{sources} · {job.apply_url or (job.source_urls or ['-'])[0]}"
    )


@app.command()
def run(
    name: Annotated[str, typer.Argument(help="Tên doanh nghiệp.")],
    tax_code: Annotated[str | None, typer.Option(help="Mã số thuế, nếu biết.")] = None,
    website: Annotated[str | None, typer.Option(help="Website, nếu biết.")] = None,
    json_out: Annotated[bool, typer.Option("--json", help="In kết quả dạng JSON.")] = False,
    show: Annotated[int, typer.Option(help="Số job in ra (dạng text).")] = 50,
) -> None:
    """Chạy pipeline cho một doanh nghiệp ngay trong process này (không qua queue)."""
    result = _run(_run_company(CompanyIn(name=name, tax_code=tax_code, website=website)))
    if json_out:
        typer.echo(result.model_dump_json(indent=2))
    else:
        incomplete = ", ".join(result.incomplete_sources) or "-"
        typer.echo(
            f"{result.company_name} [{result.status}] {result.duration_ms} ms · "
            f"KYC {result.kyc_status or '-'} · website {result.website or '-'} · "
            f"job nhận/tìm thấy {result.jobs_accepted}/{result.jobs_found} · "
            f"nguồn chưa xong: {incomplete} · run_id {result.run_id}"
        )
        for job in result.jobs[:show]:
            typer.echo(_job_line(job))
        if len(result.jobs) > show:
            typer.echo(f"  … và {len(result.jobs) - show} job khác (--show, --json).")
    if result.status is RunStatus.FAILED:
        raise typer.Exit(1)


def _company_id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        typer.secho(f"Id doanh nghiệp không hợp lệ: {value}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from None


@app.command()
def build(
    company_id: Annotated[str, typer.Argument(help="Id doanh nghiệp (UUID).")],
    refresh: Annotated[
        bool, typer.Option(help="Tìm lại nguồn trên website (như build đêm).")
    ] = True,
) -> None:
    """Build một doanh nghiệp đã nạp ngay trong process này: lượt đủ, ghi bộ khung và lịch."""
    from jobcrawler.services import crawl

    cid = _company_id(company_id)

    async def go() -> CompanyResult:
        async with open_resources() as res:
            trigger = RunTrigger.ERROR if refresh else RunTrigger.MANUAL
            return await crawl.build_company(res, cid, trigger=trigger, continue_partial=False)

    result = _run(go())
    typer.echo(
        f"{result.company_name} [{result.status}] {result.duration_ms} ms · "
        f"job nhận/tìm thấy {result.jobs_accepted}/{result.jobs_found} · run_id {result.run_id}"
    )
    if result.status is RunStatus.FAILED:
        raise typer.Exit(1)


@app.command()
def poll(
    company_id: Annotated[str, typer.Argument(help="Id doanh nghiệp (UUID).")],
    json_out: Annotated[bool, typer.Option("--json", help="In kết quả dạng JSON.")] = False,
) -> None:
    """Poll một doanh nghiệp theo bộ khung ngay trong process này (không qua queue)."""
    from jobcrawler.services import crawl

    cid = _company_id(company_id)

    async def go() -> Any:
        async with open_resources() as res:
            return await crawl.poll_company(res, cid, trigger=RunTrigger.MANUAL)

    outcome = _run(go())
    if json_out:
        typer.echo(json.dumps(outcome.as_dict(), ensure_ascii=False, indent=2))
    else:
        typer.echo(
            f"[{outcome.status}] {outcome.duration_ms} ms · mới {outcome.new} · không còn "
            f"{outcome.gone} · lưu {'có' if outcome.saved else 'không'}"
            + (" · build lại" if outcome.rebuild else "")
            + (f" · {outcome.error}" if outcome.error else "")
        )
        for source, state in outcome.sources.items():
            typer.echo(f"  {source}: {state}")
    if outcome.status in ("failed", "broken"):
        raise typer.Exit(1)


@app.command()
def kyc(
    name: Annotated[str, typer.Argument(help="Tên doanh nghiệp.")],
    tax_code: Annotated[str | None, typer.Option(help="Mã số thuế, nếu biết.")] = None,
    website: Annotated[str | None, typer.Option(help="Website, nếu biết.")] = None,
    refresh: Annotated[bool, typer.Option(help="Bỏ qua cache KYC (30 ngày).")] = False,
    json_out: Annotated[bool, typer.Option("--json", help="In kết quả dạng JSON.")] = False,
) -> None:
    """Chỉ chạy KYC: xác định pháp nhân, website, MST, alias (có cache, lưu vào DB)."""
    from jobcrawler.services import companies

    async def go() -> companies.KycReport:
        async with open_resources() as res:
            return await companies.kyc_company(
                res, CompanyIn(name=name, tax_code=tax_code, website=website), refresh=refresh
            )

    report = _run(go())
    c, step = report.company, report.step
    result = step.result
    if json_out:
        payload = {
            "company": c.model_dump(mode="json"),
            "aliases": [{"kind": k, "alias": a} for k, a in report.aliases],
            "cached": step.cached,
            "cache_hit": report.how,
            "website": step.website,
            "result": (
                {
                    k: v
                    for k, v in dataclasses.asdict(result).items()
                    if k not in ("evidence", "candidates")
                }
                | result.record().model_dump(mode="json")
                if result
                else None
            ),
        }
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return

    color = {
        "verified": typer.colors.GREEN,
        "ambiguous": typer.colors.YELLOW,
        "not_found": typer.colors.RED,
    }.get(str(c.kyc_status), typer.colors.WHITE)
    source = f"cache ({report.how})" if step.cached else "mới"
    if result is not None and result.error:
        source += f", lỗi {result.error}: chưa lưu"
    typer.echo(
        typer.style(f"[{c.kyc_status or '-'}]", fg=color)
        + f" {c.display_name} · tin cậy {c.kyc_confidence if c.kyc_confidence is not None else '-'}"
        + f" · {source}"
    )
    for label, value in [
        ("Tên pháp lý", c.legal_name),
        ("Thương hiệu", c.brand_name),
        ("MST", c.tax_code),
        ("Website", c.primary_domain),
        ("Domain khác", ", ".join(c.other_domains)),
        ("Địa chỉ", c.address),
    ]:
        if value:
            typer.echo(f"  {label}: {value}")
    if report.aliases:
        typer.echo("  Alias: " + "; ".join(f"{a} ({k})" for k, a in report.aliases))
    # Kết quả chưa lưu (hết giờ) thì in kết quả của lượt này thay vì bản đã lưu.
    unsaved = result is not None and result.error is not None
    record: dict[str, Any] = (
        result.record().model_dump(mode="json")
        if result is not None and unsaved
        else c.kyc_evidence
        if isinstance(c.kyc_evidence, dict)
        else {}
    )
    if record.get("signals"):
        typer.echo("  Tín hiệu: " + ", ".join(record["signals"]))
    if record.get("reason"):
        typer.echo(f"  Lý do: {record['reason']}")
    for ev in record.get("evidence") or []:
        typer.echo(f"  · [{ev['kind']}] {ev['snippet']} — {ev['url']}")
    for cand in record.get("candidates") or []:
        mst = f" MST {', '.join(cand['tax_codes'])}" if cand.get("tax_codes") else ""
        flags = f" [{', '.join(cand['flags'])}]" if cand.get("flags") else ""
        mark = "→" if cand["id"] == record.get("picked") else " "
        typer.echo(f"  {mark} {cand['id']} {cand['kind']} {cand['url']}{mst}{flags}")
    if result is not None:
        typer.echo(
            f"  {result.duration_ms} ms · {len(report.records)} request · SERP {result.serp or '-'}"
        )


@app.command()
def site(
    website: Annotated[str, typer.Argument(help="Domain hoặc URL, ví dụ base.vn.")],
    json_out: Annotated[bool, typer.Option("--json", help="In kết quả dạng JSON.")] = False,
    refresh: Annotated[bool, typer.Option(help="Bỏ qua cache tìm nguồn (7 ngày).")] = False,
    discover_only: Annotated[
        bool, typer.Option("--discover-only", help="Chỉ tìm trang careers/ATS.")
    ] = False,
    deadline: Annotated[
        float | None, typer.Option(help="Giây tối đa (mặc định COMPANY_DEADLINE_SECONDS).")
    ] = None,
    show: Annotated[int, typer.Option(help="Số job in ra (dạng text).")] = 50,
    steps: Annotated[bool, typer.Option(help="In các bước tìm nguồn.")] = False,
) -> None:
    """Tìm trang careers/ATS của một website và liệt kê job (chưa chuẩn hoá/xác minh)."""
    from jobcrawler.services import jobs
    from jobcrawler.sources import ats

    async def go() -> jobs.WebsiteJobs:
        async with open_resources() as res:
            limit = deadline or res.settings.company_deadline_seconds
            return await jobs.jobs_for_website(
                res,
                website,
                deadline=time.monotonic() + limit,
                refresh=refresh,
                discover_only=discover_only,
            )

    found = _run(go())
    d = found.discovery
    if json_out:
        payload = {
            "website": found.website,
            "domain": found.domain,
            "discovery": d.model_dump(mode="json") if d else None,
            "sources": [dataclasses.asdict(r) for r in found.collected.reports],
            "incomplete_sources": found.incomplete_sources,
            "jobs": [j.model_dump(mode="json") for j in found.jobs],
        }
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return

    if d is None:
        typer.secho(f"{found.domain}: hết giờ khi tìm nguồn.", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    cached = " (cache)" if d.from_cache else ""
    typer.echo(
        f"{d.domain} · trang chủ {d.homepage or '-'} · cách tìm: {d.method or '-'}{cached}"
        + (f" · lỗi: {d.error}" if d.error else "")
    )
    for url in d.careers_pages:
        typer.echo(f"  careers: {url}")
    for ref in d.ats:
        typer.echo(f"  ATS: {ref.name} {ref.url}")
    for ref in d.unsupported_ats:
        typer.echo(f"  ATS chưa thu thập được: {ref.name} — {ats.unsupported_reason(ref)}")
    if steps:
        for step in d.steps:
            typer.echo(f"  · {step}")
    if not d.found:
        typer.secho("Không tìm thấy trang careers hay ATS.", fg=typer.colors.YELLOW)
    if discover_only:
        return
    for r in found.collected.reports:
        mark = typer.style(
            f"{r.outcome.value:<8}", fg=typer.colors.GREEN if r.jobs else typer.colors.YELLOW
        )
        typer.echo(
            f"  {mark} {r.name}: {r.jobs} job, {r.duration_ms / 1000:.1f} s"
            + (f" · {r.error}" if r.error else "")
        )
    typer.echo(f"Job ({len(found.jobs)}):")
    for job in found.jobs[:show]:
        where = "; ".join(job.locations) or "-"
        typer.echo(f"  [{job.method.value}] {job.title} — {where} — {job.url}")
    if len(found.jobs) > show:
        typer.echo(f"  … và {len(found.jobs) - show} job khác (--show, --json).")
    if found.incomplete_sources:
        typer.echo(f"Chưa xong: {', '.join(found.incomplete_sources)}")


@app.command()
def runner(
    role: Annotated[
        list[str] | None,
        typer.Option(
            "--role",
            help="Vai chạy (lặp được): build, poll, boards. Mặc định cả ba trong một process.",
        ),
    ] = None,
) -> None:
    """Chạy job trong hàng đợi: `build` (lượt đủ, ghi bộ khung + cron bảo trì), `poll` (lượt nhẹ
    theo bộ khung), `boards` (index trang tuyển dụng). Cần gateway + worker lấy trang."""
    from jobcrawler.entrypoints.runner import ROLES, run_runner

    roles = role or list(ROLES)
    unknown = [r for r in roles if r not in ROLES]
    if unknown:
        typer.secho(f"Vai không hợp lệ: {', '.join(unknown)}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    _run(run_runner(get_settings(), roles))  # pyright: ignore[reportArgumentType]


@app.command()
def scheduler() -> None:
    """Xếp lịch: build bộ khung ban đêm (rải đều trong BUILD_WINDOW), poll tin mới ban ngày mỗi
    POLL_INTERVAL_SECONDS. Chạy nhiều replica được (chỉ một replica giữ khoá làm việc)."""
    from jobcrawler.entrypoints.scheduler import run_scheduler

    _run(run_scheduler(get_settings()))


@app.command()
def gateway() -> None:
    """Gateway gRPC cho worker lấy trang (GATEWAY_HOST:GATEWAY_PORT)."""
    from jobcrawler.entrypoints.gateway import run_gateway

    _run(run_gateway(get_settings()))


def _local_gateway_addr(host: str, port: int) -> str:
    """Địa chỉ gọi gateway từ chính máy / container của nó (0.0.0.0, :: → loopback)."""
    host = host.strip("[]")
    if host in ("", "0.0.0.0"):
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


@app.command("gateway-health")
def gateway_health(
    addr: Annotated[
        str | None, typer.Option(help="host:port của gateway (mặc định 127.0.0.1:GATEWAY_PORT).")
    ] = None,
    service: Annotated[str, typer.Option(help="Service cần hỏi; trống = cả server.")] = "",
    timeout: Annotated[float, typer.Option(help="Số giây chờ trả lời.")] = 5.0,
    ca_cert: Annotated[
        str | None,
        typer.Option(help="CA (PEM) xác thực TLS; mặc định chính GATEWAY_TLS_CERT (tự ký)."),
    ] = None,
    server_name: Annotated[
        str | None, typer.Option(help="Tên trong chứng chỉ TLS nếu khác host của --addr.")
    ] = None,
) -> None:
    """Hỏi health gRPC (grpc.health.v1) của gateway; exit 0 khi SERVING (healthcheck Docker).
    Dùng TLS khi đặt GATEWAY_TLS_CERT/KEY; health check không cần GATEWAY_TOKEN."""
    from pathlib import Path

    import grpc
    from grpc_health.v1 import health_pb2, health_pb2_grpc

    settings = get_settings()
    target = addr or _local_gateway_addr(settings.gateway_host, settings.gateway_port)
    if settings.gateway_tls_cert and settings.gateway_tls_key:
        try:
            root = Path(ca_cert or settings.gateway_tls_cert).read_bytes()
        except OSError as exc:
            typer.secho(f"Không đọc được chứng chỉ: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from None
        options = [("grpc.ssl_target_name_override", server_name)] if server_name else []
        credentials = grpc.ssl_channel_credentials(root_certificates=root)
        channel = grpc.secure_channel(target, credentials, options=options)
    else:
        channel = grpc.insecure_channel(target)
    with channel:
        stub = health_pb2_grpc.HealthStub(channel)
        try:
            request = health_pb2.HealthCheckRequest(service=service)
            response = stub.Check(request, timeout=timeout)  # pyright: ignore[reportAttributeAccessIssue]
        except grpc.RpcError as exc:
            # Lỗi thật của kênh đồng bộ cũng là grpc.Call: có code() / details().
            call: Any = exc
            reason = call.code().name if hasattr(call, "code") else type(exc).__name__
            message = (call.details() or "") if hasattr(call, "details") else ""
            typer.secho(f"{target}: {reason} {message}".rstrip(), fg=typer.colors.RED, err=True)
            raise typer.Exit(1) from None
    status = health_pb2.HealthCheckResponse.ServingStatus.Name(response.status)
    if response.status != health_pb2.HealthCheckResponse.SERVING:
        typer.secho(f"{target}: {status}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    typer.echo(f"{status} {target}")


@app.command()
def ui(
    host: Annotated[str, typer.Option(help="Địa chỉ lắng nghe (0.0.0.0 trong container).")] = (
        "127.0.0.1"
    ),
    port: Annotated[int, typer.Option(help="Cổng.")] = 8000,
    reload: Annotated[bool, typer.Option(help="Tự nạp lại khi sửa code (dev).")] = False,
) -> None:
    """Giao diện web + API JSON (đăng nhập bằng UI_USERNAME / UI_PASSWORD nếu đặt)."""
    import uvicorn

    uvicorn.run(
        "jobcrawler.web.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        proxy_headers=True,
        log_level=get_settings().log_level.lower(),
    )


# --- doanh nghiệp, queue -----------------------------------------------------------------------


def _read_csv(path: str) -> Any:
    from jobcrawler.services import batch

    try:
        report = batch.read_companies_csv(path)
    except (OSError, batch.CsvFormatError) as exc:
        typer.secho(f"Không đọc được {path}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from None
    for message in report.errors[:20]:
        typer.secho(f"  bỏ {message}", fg=typer.colors.RED, err=True)
    for message in report.warnings[:20]:
        typer.secho(f"  {message}", fg=typer.colors.YELLOW, err=True)
    hidden = len(report.errors) + len(report.warnings) - 40
    if hidden > 0:
        typer.echo(f"  … và {hidden} dòng cảnh báo/lỗi khác", err=True)
    return report


@app.command("import")
def import_companies(path: Annotated[str, typer.Argument(help="File companies.csv.")]) -> None:
    """Nạp danh sách doanh nghiệp từ CSV (cột name, tax_code, website, note) vào `companies`
    (chưa chạy KYC). Chạy lại không tạo trùng."""
    from jobcrawler.services import batch

    report = _read_csv(path)

    async def go() -> batch.ImportResult:
        async with open_resources(db_logs=False) as res:
            return await batch.import_companies(res.sessionmaker, [r.company for r in report.rows])

    result = _run(go())
    typer.echo(
        f"{len(report.rows)} dòng hợp lệ · tạo mới {result.created} · cập nhật {result.updated}"
        f" · giữ nguyên {result.unchanged} · trùng trong file {report.duplicates}"
        f" · bỏ {len(report.errors)}"
    )


@app.command()
def enqueue(
    all_: Annotated[bool, typer.Option("--all", help="Mọi doanh nghiệp trong DB.")] = False,
    file: Annotated[
        str | None, typer.Option("--file", help="companies.csv (được import trước).")
    ] = None,
    stale_hours: Annotated[
        float | None,
        typer.Option(
            "--stale-hours", help="Chỉ doanh nghiệp chưa có lượt chạy xong trong N giờ qua."
        ),
    ] = None,
    limit: Annotated[int | None, typer.Option(help="Tối đa N doanh nghiệp.")] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Chỉ đếm, không đưa vào queue.")
    ] = False,
) -> None:
    """Đưa doanh nghiệp vào queue build (mỗi doanh nghiệp một job `build_company`, timeout
    COMPANY_TIMEOUT_SECONDS; job cùng doanh nghiệp đang chờ/chạy thì bỏ qua)."""
    from jobcrawler.services import batch

    if all_ and file:
        typer.secho("Chọn --all hoặc --file, không dùng cả hai.", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    if not (all_ or file or stale_hours is not None):
        typer.secho("Chọn --all, --file hoặc --stale-hours.", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    report = _read_csv(file) if file else None

    async def go() -> tuple[int, batch.EnqueueResult | None, dict[str, int]]:
        async with open_resources(db_logs=False) as res:
            ids = None
            if report is not None:
                imported = await batch.import_companies(
                    res.sessionmaker, [r.company for r in report.rows]
                )
                ids = imported.company_ids
            async with res.sessionmaker() as session:
                companies = await batch.select_companies(
                    session, company_ids=ids, stale_hours=stale_hours, limit=limit
                )
            result = None if dry_run else await batch.enqueue_companies(res, companies)
            info = await res.queues.build.info()
            return len(companies), result, {"queued": info["queued"], "active": info["active"]}

    selected, result, queue = _run(go())
    if result is None:
        typer.echo(f"{selected} doanh nghiệp sẽ được đưa vào queue (--dry-run).")
    else:
        typer.echo(
            f"đưa vào queue {result.enqueued}/{selected} doanh nghiệp"
            f" ({result.already_queued} đã có job đang chờ/chạy)"
        )
    typer.echo(f"queue: {queue['queued']} đang chờ · {queue['active']} đang chạy")


@app.command("index-boards")
def index_boards(
    source: Annotated[
        list[str] | None, typer.Option("--source", help="Tên trang (lặp được), vd. topcv.")
    ] = None,
    max_jobs: Annotated[
        int | None,
        typer.Option(help="Số trang chi tiết tối đa mỗi trang (BOARD_MAX_JOBS_PER_RUN)."),
    ] = None,
    budget_minutes: Annotated[
        float | None, typer.Option(help="Thời gian tối đa mỗi trang (BOARD_INDEX_BUDGET_SECONDS).")
    ] = None,
    full: Annotated[bool, typer.Option(help="Tải lại mọi tin, bỏ qua lastmod/ETag.")] = False,
    tail: Annotated[
        bool,
        typer.Option(
            "--tail",
            help="Lượt đuôi (như cron ban ngày): chỉ phần mới nhất của danh sách, chỉ tin chưa có.",
        ),
    ] = False,
    map_only: Annotated[
        bool, typer.Option("--map-only", help="Chỉ map lại profile ↔ doanh nghiệp.")
    ] = False,
    rematch: Annotated[
        bool,
        typer.Option(
            "--rematch",
            help="Tính lại khoá tên profile + alias khớp, thử lại mọi profile chưa map rồi map.",
        ),
    ] = False,
    list_boards: Annotated[bool, typer.Option("--list", help="Liệt kê các trang.")] = False,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Index tăng dần các trang tuyển dụng vào job_postings + source_profiles, rồi map profile
    (`--tail`: lượt đuôi, chỉ tin mới nhất)."""
    from jobcrawler.services import boards as board_service
    from jobcrawler.sources import boards

    if list_boards:
        for adapter in boards.registry().values():
            state = "bật" if adapter.enabled else "tắt"
            typer.echo(f"{adapter.name:<14} {state:<4} {adapter.home} {adapter.note}")
        return
    try:
        board_service.selected_boards(get_settings(), source)
    except board_service.UnknownBoardError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from None

    if rematch:

        async def go_rematch() -> board_service.RematchReport:
            async with open_resources() as res:
                return await board_service.rematch_profiles(res, source)

        again = _run(go_rematch())
        typer.echo(
            f"khoá tên mới {again.renamed} profile · alias khớp mới {again.aliases}"
            f" · map được {again.mapped.matched} profile mới"
            f" · tổng {again.mapped.mapped_total}/{again.mapped.profiles} profile đã map"
        )
        return

    if map_only:

        async def go_map() -> board_service.MapReport:
            async with open_resources() as res:
                return await board_service.map_all_profiles(res, source)

        report = _run(go_map())
        typer.echo(
            f"map được {report.matched} profile mới · tổng {report.mapped_total}/{report.profiles}"
            " profile đã map"
        )
        return

    async def go() -> list[Any]:
        async with open_resources() as res:
            return await board_service.index_boards(
                res,
                source,
                max_jobs=max_jobs,
                budget_seconds=budget_minutes * 60 if budget_minutes else None,
                full=full,
                tail=tail,
            )

    results = _run(go())
    if json_out:
        typer.echo(
            json.dumps(
                [dataclasses.asdict(r) for r in results], ensure_ascii=False, indent=2, default=str
            )
        )
    else:
        for r in results:
            color = {"ok": typer.colors.GREEN, "partial": typer.colors.YELLOW}.get(
                r.status.value, typer.colors.RED
            )
            typer.echo(
                typer.style(f"{r.source:<14} {r.status.value:<8}", fg=color)
                + f" thấy {r.urls_seen} · tải {r.fetched}/{r.to_fetch} · mới {r.new}"
                f" · đổi {r.updated} · không đổi {r.unchanged} · lỗi {r.failed}"
                f" · gỡ {r.removed} · profile mới {r.profiles_new} · map {r.profiles_matched}"
                f" · {r.duration_ms / 1000:.0f} s"
                + (" · danh sách chưa đủ" if not r.complete else "")
                + (" · còn tin chưa tải" if r.truncated else "")
                + (f" · {r.error or r.skipped}" if (r.error or r.skipped) else "")
            )
    if any(r.status.value == "failed" for r in results):
        raise typer.Exit(1)


@app.command()
def export(
    format_: Annotated[str, typer.Option("--format", help="jsonl (JobOut) hoặc csv.")] = "jsonl",
    out: Annotated[
        str, typer.Option("--out", help="Thư mục (ghi jobs.<format>) hoặc đường dẫn file.")
    ] = "out/",
    company: Annotated[
        list[str] | None,
        typer.Option("--company", help="Id (UUID), MST hoặc tên doanh nghiệp (lặp được)."),
    ] = None,
    all_status: Annotated[
        bool, typer.Option("--all", help="Gồm cả job đã hết hạn / đã gỡ.")
    ] = False,
) -> None:
    """Export job đã chuẩn hoá ra JSONL (mỗi dòng một JobOut) hoặc CSV."""
    from jobcrawler.services import export as export_service

    if format_ not in export_service.FORMATS:
        typer.secho(f"--format phải là {' | '.join(export_service.FORMATS)}", fg="red", err=True)
        raise typer.Exit(2)
    fmt: export_service.ExportFormat = "csv" if format_ == "csv" else "jsonl"

    async def go() -> export_service.ExportResult:
        async with open_resources(db_logs=False) as res:
            return await export_service.export_jobs(
                res, fmt=fmt, out=out, companies=company or (), include_inactive=all_status
            )

    try:
        result = _run(go())
    except export_service.UnknownCompanyError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from None
    typer.echo(f"{result.count} job → {result.path}")


@app.command()
def stats(
    hours: Annotated[
        float, typer.Option(help="Cửa sổ thời gian (giờ) cho các lượt chạy; 0 = tất cả.")
    ] = 24,
    no_storage: Annotated[
        bool, typer.Option("--no-storage", help="Bỏ phần đo dung lượng nén (nhanh hơn).")
    ] = False,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Thống kê: queue, lượt chạy (p50/p95, tỉ lệ failed), nguồn, index trang tuyển dụng, dung
    lượng DB và tỉ lệ nén."""
    from jobcrawler.services import stats as stats_service

    async def go() -> stats_service.Stats:
        async with open_resources(db_logs=False) as res:
            return await stats_service.collect_stats(
                res, hours=hours or None, storage=not no_storage
            )

    result = _run(go())
    if json_out:
        typer.echo(
            json.dumps(dataclasses.asdict(result), ensure_ascii=False, indent=2, default=str)
        )
        return
    for line in _stats_lines(result):
        typer.echo(line)


def _mb(size: int) -> str:
    return f"{size / 1024 / 1024:.1f} MB"


def _stats_lines(st: Any) -> list[str]:
    def fmt_s(v: float | None) -> str:
        return "-" if v is None else f"{v:.0f} s"

    runs = st.runs
    window = f"từ {runs.since:%Y-%m-%d %H:%M} UTC" if runs.since else "toàn bộ"
    lines = ["== Queue"]
    for q in st.queues:
        if q.error:
            lines.append(f"  {q.name}: lỗi {q.error}")
            continue
        lines.append(
            f"  {q.name}: chờ {q.queued} · chạy {q.active} · hẹn giờ {q.scheduled}"
            f" · {q.workers} worker (xong {q.complete}, lỗi {q.failed}, chạy lại {q.retried},"
            f" huỷ {q.aborted})"
        )
    kyc = ", ".join(
        f"{'chưa KYC' if k == 'none' else k} {v}" for k, v in sorted(st.kyc_status.items())
    )
    jobs = ", ".join(f"{k} {v}" for k, v in sorted(st.jobs_by_status.items()))
    lines += [
        f"== Doanh nghiệp: {st.companies} ({kyc})",
        f"== Job: {jobs or '-'}"
        + (
            " · đang mở theo nguồn: " + ", ".join(f"{s} {n}" for s, n in st.jobs_by_source[:8])
            if st.jobs_by_source
            else ""
        ),
        f"== Lượt chạy ({window}): {runs.runs} lượt, {runs.companies} doanh nghiệp"
        + (f", {runs.per_hour}/giờ" if runs.per_hour else ""),
        "  trạng thái: "
        + (", ".join(f"{k} {v}" for k, v in sorted(runs.by_status.items())) or "-"),
        "  lượt mới nhất mỗi doanh nghiệp: "
        + (", ".join(f"{k} {v}" for k, v in sorted(runs.latest_by_status.items())) or "-")
        + f" · có job: {runs.companies_with_jobs}",
        f"  thời gian: p50 {fmt_s(runs.p50_seconds)} · p95 {fmt_s(runs.p95_seconds)}"
        f" · max {fmt_s(runs.max_seconds)} · quá timeout {runs.over_timeout}",
        f"  chờ trong hàng đợi: p50 {fmt_s(runs.wait_p50_seconds)}"
        f" · p95 {fmt_s(runs.wait_p95_seconds)}",
        f"  job tìm thấy {runs.jobs_found} · nhận {runs.jobs_accepted}",
    ]
    if runs.incomplete_sources:
        lines.append(
            "  nguồn chưa xong: " + ", ".join(f"{s} {n}" for s, n in runs.incomplete_sources)
        )
    for error, n in runs.errors[:5]:
        lines.append(f"  lỗi ×{n}: {error}")
    if st.sources:
        lines.append("== Nguồn (lượt chạy doanh nghiệp)")
        for src in st.sources[:15]:
            outcomes = ", ".join(f"{k} {v}" for k, v in sorted(src.outcomes.items()))
            lines.append(
                f"  {src.kind:<16} {src.fetches:>6} request · {outcomes} · tb {src.avg_ms} ms"
            )
    if st.boards:
        lines.append("== Trang tuyển dụng")
        for b in st.boards:
            last = (
                f"lượt cuối {b.last_status} {b.last_finished or b.last_started:%Y-%m-%d %H:%M}"
                f" thấy {b.last_urls_seen} tải {b.last_fetched} mới {b.last_new}"
                f" gỡ {b.last_removed}" + ("" if b.last_complete else " (danh sách chưa đủ)")
                if b.last_status
                else "chưa index"
            )
            lines.append(
                f"  {b.source:<13} tin mở {b.active_postings}/{b.postings} · profile map"
                f" {b.profiles_mapped}/{b.profiles} · {last}"
            )
    if st.storage:
        lines.append("== Nén")
        for p in st.storage:
            dicts = ", ".join(
                f"{'không từ điển' if k == 0 else k}: {n}" for k, n in (p.by_dict or {}).items()
            )
            lines.append(
                f"  {p.table}.{p.kind}: {p.values} giá trị · lưu {_mb(p.stored_bytes)}"
                f" (gốc ~{_mb(p.original_bytes)}, x{p.ratio or '-'}) · mẫu theo từ điển: {dicts}"
            )
    lines.append(
        "== Dung lượng: " + ", ".join(f"{name} {_mb(size)}" for name, size in st.tables[:8])
    )
    lines.append("== Mục tiêu")
    for t in st.targets:
        value = (
            "-"
            if t.value is None
            else (f"{t.value:.1%}" if t.kind == "rate" else f"{t.value:.0f} s")
        )
        state = {True: "đạt", False: "CHƯA ĐẠT", None: "chưa có dữ liệu"}[t.ok]
        lines.append(f"  {t.name}: {value} (mục tiêu {t.target}) → {state}")
    return lines


@app.command("eval")
def eval_(
    golden: Annotated[str, typer.Argument(help="Thư mục golden set.")] = "golden/",
    out: Annotated[str, typer.Option(help="Thư mục ghi báo cáo.")] = "out/eval",
    sample: Annotated[
        int, typer.Option(help="Chưa có golden set: số job ngẫu nhiên để kiểm tay.")
    ] = 50,
    seed: Annotated[int | None, typer.Option(help="Seed chọn mẫu (lặp lại được).")] = None,
    reviewed: Annotated[
        str | None,
        typer.Option(help="File mẫu đã điền cột `dung` (y/n): tính precision từ đó."),
    ] = None,
    all_status: Annotated[
        bool, typer.Option("--all", help="So cả job đã hết hạn / đã gỡ.")
    ] = False,
) -> None:
    """Precision/recall theo doanh nghiệp và theo nguồn trên golden set (companies.csv +
    jobs.csv). Chưa có golden set: in và ghi N job ngẫu nhiên để kiểm tay."""
    from pathlib import Path

    from jobcrawler.services import evaluate

    if reviewed:
        review = evaluate.read_review(reviewed)
        typer.echo(
            f"đã kiểm {review.reviewed} job · đúng {review.correct} · precision"
            f" {evaluate.pct(review.precision)} (mục tiêu ≥ {evaluate.TARGET_PRECISION:.0%})"
            + (f" · {review.unknown} dòng chưa điền" if review.unknown else "")
        )
        return

    out_dir = Path(out)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    try:
        golden_set = evaluate.load_golden(golden)
    except evaluate.GoldenError as exc:
        golden_set = None
        typer.secho(f"Chưa có golden set ({exc}): lấy {sample} job ngẫu nhiên.", err=True)

    if golden_set is None:

        async def go_sample() -> list[evaluate.SampleJob]:
            async with open_resources(db_logs=False) as res, res.sessionmaker() as session:
                return await evaluate.sample_jobs(session, n=sample, seed=seed)

        jobs = _run(go_sample())
        csv_path = out_dir / f"sample-{stamp}.csv"
        md_path = out_dir / f"sample-{stamp}.md"
        evaluate.write_sample_csv(csv_path, jobs)
        from datetime import UTC, datetime

        text = evaluate.sample_markdown(jobs, generated_at=datetime.now(UTC), csv_path=csv_path)
        md_path.write_text(text, encoding="utf-8")
        typer.echo(text)
        typer.echo(
            f"→ {md_path} · điền cột `dung` trong {csv_path} rồi: jobcrawler eval"
            f" --reviewed {csv_path.as_posix()}"
        )
        return

    async def go() -> tuple[evaluate.EvalReport, dict[str, Any]]:
        async with open_resources(db_logs=False) as res:
            report = await evaluate.evaluate(res, golden_set, include_inactive=all_status)
            ids = [c.company_id for c in report.companies if c.company_id is not None]
            async with res.sessionmaker() as session:
                perf = await evaluate.latest_run_perf(session, ids)
            return report, perf

    report, perf = _run(go())
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"eval-{stamp}.md"
    text = evaluate.markdown_report(report, perf=perf)
    md_path.write_text(text, encoding="utf-8")
    (out_dir / f"eval-{stamp}.json").write_text(evaluate.report_json(report), encoding="utf-8")
    typer.echo(text)
    typer.echo(f"→ {md_path}")


# --- hạ tầng ----------------------------------------------------------------------------------


@proxies_app.command("check")
def proxies_check(
    url: Annotated[
        str | None, typer.Option(help="URL kiểm tra (mặc định PROXY_CHECK_URL).")
    ] = None,
    concurrency: Annotated[int, typer.Option(min=1)] = 20,
    timeout: Annotated[float, typer.Option(help="Giây cho mỗi proxy.")] = 15,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Gọi thử qua từng proxy trong PROXIES_FILE, cập nhật trạng thái pool trên Redis."""
    from jobcrawler.services import health

    async def go() -> list[health.ProxyCheck] | None:
        async with open_resources() as res:
            pool = health.proxies_to_check(res)
            if not pool.enabled:
                return None
            return await health.check_proxies(
                res, pool=pool, url=url, concurrency=concurrency, timeout=timeout
            )

    settings = get_settings()
    if settings.proxy_mode != "central":
        typer.secho(
            f"PROXY_MODE={settings.proxy_mode}: backend không chọn proxy; chỉ kiểm tra danh sách"
            " proxy chung.",
            fg=typer.colors.YELLOW,
            err=True,
        )
    results = _run(go())
    if results is None:
        typer.secho(
            f"Không có proxy nào trong {settings.proxies_file}.", fg=typer.colors.YELLOW, err=True
        )
        raise typer.Exit(1)
    if json_out:
        typer.echo(
            json.dumps([dataclasses.asdict(r) for r in results], ensure_ascii=False, indent=2)
        )
    else:
        for r in sorted(results, key=lambda r: (not r.ok, r.latency_ms or 0)):
            mark = (
                typer.style("OK  ", fg=typer.colors.GREEN)
                if r.ok
                else typer.style("FAIL", fg=typer.colors.RED)
            )
            detail = f"{r.latency_ms} ms  ip={r.exit_ip} loc={r.location}" if r.ok else r.error
            typer.echo(f"{mark} {r.proxy_id}  {r.proxy}  {detail}")
        ok = sum(r.ok for r in results)
        typer.echo(f"{ok}/{len(results)} proxy hoạt động.")
    if not any(r.ok for r in results):
        raise typer.Exit(1)


@llm_app.command("check")
def llm_check(json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Gọi thử LLM với structured output (JSON schema)."""
    from jobcrawler.services import health

    async def go() -> tuple[Any, bool, bool]:
        async with open_resources() as res:
            result, correct = await health.check_llm(res)
            return result, correct, res.llm.enabled

    result, correct, enabled = _run(go())
    if not enabled:
        typer.secho("Chưa cấu hình LLM_BASE_URL / LLM_MODEL.", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(1)
    if json_out:
        payload = dataclasses.asdict(result) | {"correct": correct}
        typer.echo(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        status = (
            typer.style("OK", fg=typer.colors.GREEN)
            if correct
            else typer.style("LỖI", fg=typer.colors.RED)
        )
        typer.echo(
            f"{status} model={result.model} {result.duration_ms} ms "
            f"tokens={result.prompt_tokens}+{result.completion_tokens} attempts={result.attempts}"
        )
        typer.echo(
            f"answer={result.value.model_dump() if result.value else None} error={result.error}"
        )
    if not correct:
        raise typer.Exit(1)


@app.command()
def fetch(
    url: Annotated[str, typer.Argument()],
    render: Annotated[bool, typer.Option("--render", help="Mở bằng Chromium.")] = False,
    robots: Annotated[bool, typer.Option(help="Tôn trọng robots.txt.")] = True,
    cache: Annotated[bool, typer.Option(help="Dùng HTTP cache trên Redis.")] = True,
    show: Annotated[int, typer.Option(help="Số ký tự nội dung in ra.")] = 500,
) -> None:
    """Fetch thử một URL qua toàn bộ tầng mạng (proxy, rate limit, robots, retry) để debug."""
    from jobcrawler.services import health

    async def go() -> Any:
        async with open_resources() as res:
            return await health.probe_url(
                res, url, render=render, check_robots=robots, use_cache=cache
            )

    result = _run(go())
    typer.echo(
        f"{result.outcome} status={result.status} {result.duration_ms} ms "
        f"attempts={result.attempts} proxy={result.proxy_id} cache={result.from_cache} "
        f"rendered={result.rendered} "
        f"final_url={result.final_url} error={result.error}"
    )
    typer.echo(f"content-type={result.headers.get('content-type')} bytes={len(result.content)}")
    if show and result.content:
        typer.echo(result.text[:show])
    if not result.ok:
        raise typer.Exit(1)


@queue_app.command("info")
def queue_info(json_out: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Số job đang chờ / chạy / hẹn giờ và số runner của từng hàng đợi."""

    async def go() -> list[dict[str, Any]]:
        async with open_resources(db_logs=False) as res:
            out = []
            for queue in res.queues.all():
                info = await queue.info()
                out.append(
                    {
                        "name": queue.name,
                        "queued": info["queued"],
                        "active": info["active"],
                        "scheduled": info["scheduled"],
                        "runners": len(info["workers"]),
                    }
                )
            return out

    rows = _run(go())
    if json_out:
        typer.echo(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    for r in rows:
        typer.echo(
            f"{r['name']}: {r['queued']} chờ · {r['active']} chạy · {r['scheduled']} hẹn giờ"
            f" · {r['runners']} runner"
        )


def main() -> None:
    # Console/pipe trên Windows có thể không phải UTF-8; tên doanh nghiệp có dấu không được vỡ.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None and (stream.encoding or "").lower() != "utf-8":
            reconfigure(encoding="utf-8", errors="replace")
    app()


if __name__ == "__main__":
    main()
