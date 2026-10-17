"""CLI `jobcrawler`. Chỉ parse tham số, mở tài nguyên và gọi vào `jobcrawler.services`."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
import time
from collections.abc import Coroutine
from typing import Annotated, Any, NoReturn

import typer

from jobcrawler.config import get_settings
from jobcrawler.logging import configure_logging
from jobcrawler.resources import ResourceUnavailableError, open_resources
from jobcrawler.schemas import CompanyIn, CompanyResult, JobOut, RunStatus

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
app.add_typer(db_app, name="db")
app.add_typer(proxies_app, name="proxies")
app.add_typer(llm_app, name="llm")


@app.callback()
def _setup(
    no_proxy: Annotated[
        bool,
        typer.Option("--no-proxy", help="Không dùng proxy, đi thẳng (như PROXY_ENABLED=false)."),
    ] = False,
) -> None:
    if no_proxy:
        os.environ["PROXY_ENABLED"] = "false"
        get_settings.cache_clear()
    configure_logging(get_settings())


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    try:
        return asyncio.run(coro)
    except ResourceUnavailableError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        typer.secho("Gợi ý: docker compose up -d", err=True)
        raise typer.Exit(2) from None


def _not_implemented(milestone: str) -> NoReturn:
    typer.secho(f"Chưa cài đặt (milestone {milestone}).", fg=typer.colors.YELLOW, err=True)
    raise typer.Exit(1)


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
def worker() -> None:
    """Chạy worker SAQ (xử lý `process_company` và cron)."""
    from jobcrawler.entrypoints.worker import run_worker

    _run(run_worker(get_settings()))


# --- chưa cài đặt ------------------------------------------------------------------------------


@app.command("import")
def import_companies(path: Annotated[str, typer.Argument(help="File companies.csv.")]) -> None:
    """Nạp danh sách doanh nghiệp từ CSV."""
    _not_implemented("M6")


@app.command()
def enqueue(
    all_: Annotated[bool, typer.Option("--all")] = False,
    file: Annotated[str | None, typer.Option("--file")] = None,
    stale_hours: Annotated[int | None, typer.Option("--stale-hours")] = None,
) -> None:
    """Đưa doanh nghiệp vào queue."""
    _not_implemented("M6")


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
    map_only: Annotated[
        bool, typer.Option("--map-only", help="Chỉ map lại profile ↔ doanh nghiệp.")
    ] = False,
    list_boards: Annotated[bool, typer.Option("--list", help="Liệt kê các trang.")] = False,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Index tăng dần các trang tuyển dụng vào job_postings + source_profiles, rồi map profile."""
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
            )

    results = _run(go())
    if json_out:
        typer.echo(
            json.dumps([dataclasses.asdict(r) for r in results], ensure_ascii=False, indent=2)
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
        typer.Option("--company", help="Id hoặc tên doanh nghiệp (lặp được)."),
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
def stats() -> None:
    """Thống kê các lượt chạy."""
    _not_implemented("M6")


@app.command("eval")
def eval_(golden: Annotated[str, typer.Argument(help="Thư mục golden set.")] = "golden/") -> None:
    """Tính precision/recall trên golden set."""
    _not_implemented("M6")


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
    if not settings.proxy_enabled:
        typer.secho(
            "PROXY_ENABLED=false: crawl đang đi thẳng; chỉ kiểm tra proxy trong file.",
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


def main() -> None:
    # Console/pipe trên Windows có thể không phải UTF-8; tên doanh nghiệp có dấu không được vỡ.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None and (stream.encoding or "").lower() != "utf-8":
            reconfigure(encoding="utf-8", errors="replace")
    app()


if __name__ == "__main__":
    main()
