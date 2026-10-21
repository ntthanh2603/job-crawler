from __future__ import annotations

from pathlib import Path
from typing import Any

import grpc
import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc
from typer.testing import CliRunner

from jobcrawler.bootstrap.cli import app

runner = CliRunner()


def test_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("run", "worker", "import", "enqueue", "index-boards", "export", "stats"):
        assert command in result.output
    assert "eval" in result.output
    assert "db" in result.output
    assert "proxies" in result.output
    assert "llm" in result.output
    assert "fetch" in result.output
    assert "site" in result.output
    assert "api" in result.output
    assert "│ ui " not in result.output  # tên cũ của `api`: ẩn khỏi --help


@pytest.mark.parametrize("command", ["api", "ui"])
def test_api_serves_with_uvicorn_behind_proxy(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """`api` (và tên cũ `ui`) chạy uvicorn với proxy headers; IP proxy được tin chỉnh được."""
    import uvicorn

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append({"app": app, **kw}))
    result = runner.invoke(app, [command, "--host", "0.0.0.0", "--port", "8001"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, [command, "--forwarded-allow-ips", "172.16.0.0/12"])
    assert result.exit_code == 0, result.output
    first, second = calls
    assert first["app"] == "jobcrawler.bootstrap.app:create_app"
    assert (first["factory"], first["host"], first["port"]) == (True, "0.0.0.0", 8001)
    assert (first["proxy_headers"], first["forwarded_allow_ips"]) == (True, None)
    assert (second["host"], second["port"]) == ("127.0.0.1", 8000)
    assert second["forwarded_allow_ips"] == "172.16.0.0/12"


def test_infra_subcommands_have_check() -> None:
    for group in ("proxies", "llm"):
        result = runner.invoke(app, [group, "--help"])
        assert result.exit_code == 0
        assert "check" in result.output


def test_db_help_lists_subcommands() -> None:
    result = runner.invoke(app, ["db", "--help"])
    assert result.exit_code == 0
    for command in ("upgrade", "downgrade", "current", "revision", "maintain"):
        assert command in result.output


def test_import_reports_unreadable_file(tmp_path: Path) -> None:
    missing = runner.invoke(app, ["import", str(tmp_path / "khong-co.csv")])
    assert missing.exit_code == 2
    assert "Không đọc được" in missing.output
    bad = tmp_path / "bad.csv"
    bad.write_text("ten_khac,mst\nACME,0101234567\n", encoding="utf-8")
    result = runner.invoke(app, ["import", str(bad)])
    assert result.exit_code == 2
    assert "thiếu cột `name`" in result.output


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["enqueue"], "Chọn --all, --file hoặc --stale-hours"),
        (["enqueue", "--all", "--file", "x.csv"], "không dùng cả hai"),
    ],
)
def test_enqueue_needs_a_selection(args: list[str], message: str) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == 2
    assert message in result.output


def test_build_passes_refresh_and_runs_as_manual(monkeypatch: pytest.MonkeyPatch) -> None:
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from uuid import uuid4

    from jobcrawler.bootstrap import cli
    from jobcrawler.common.schemas import RunStatus, RunTrigger
    from jobcrawler.modules.crawl import crawl_service

    calls: list[dict[str, Any]] = []

    @asynccontextmanager
    async def resources(**kw: Any) -> AsyncIterator[str]:
        yield "res"

    async def build_company(res: Any, company_id: Any, **kw: Any) -> Any:
        calls.append(kw)
        return SimpleNamespace(
            company_name="ACME",
            status=RunStatus.OK,
            duration_ms=1,
            jobs_accepted=0,
            jobs_found=0,
            run_id=None,
        )

    monkeypatch.setattr(cli, "open_resources", resources)
    monkeypatch.setattr(crawl_service, "build_company", build_company)
    company_id = str(uuid4())
    for args in (["build", company_id], ["build", company_id, "--no-refresh"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output
    # Lượt build tay ghi trigger `manual` (không phải `error`); --refresh / --no-refresh đi tới
    # build_company.
    manual = {"trigger": RunTrigger.MANUAL, "continue_partial": False}
    assert calls == [{**manual, "refresh": True}, {**manual, "refresh": False}]


def test_reviewed_sample_gives_precision(tmp_path: Path) -> None:
    path = tmp_path / "sample.csv"
    path.write_text(
        "job_id,title,dung\n1,Kế toán,y\n2,Lập trình,đúng\n3,Bán hàng,n\n4,Kho,\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["eval", "--reviewed", str(path)])
    assert result.exit_code == 0
    assert "đã kiểm 3 job · đúng 2 · precision 66.7%" in result.output
    assert "1 dòng chưa điền" in result.output


def test_no_proxy_flag_disables_proxies(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from jobcrawler.common.config import get_settings

    monkeypatch.delenv("PROXY_MODE", raising=False)  # để monkeypatch dọn lại sau test
    get_settings.cache_clear()
    try:
        result = runner.invoke(app, ["--no-proxy", "import", str(tmp_path / "x.csv")])
        assert result.exit_code == 2  # file không có; chỉ cần callback đã chạy
        assert get_settings().proxy_mode == "off"
    finally:
        get_settings.cache_clear()


def test_index_boards_rejects_unknown_source() -> None:
    result = runner.invoke(app, ["index-boards", "--source", "khong-co"])
    assert result.exit_code == 2
    assert "không có adapter khong-co" in result.output


# --- gateway-health ------------------------------------------------------------------------------


class _Channel:
    """Kênh giả: không mở kết nối nào."""

    def __init__(self, target: str, **kwargs: Any) -> None:
        self.target = target
        self.kwargs = kwargs

    def __enter__(self) -> _Channel:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class _RpcFailure(grpc.RpcError):
    def code(self) -> grpc.StatusCode:
        return grpc.StatusCode.UNAVAILABLE

    def details(self) -> str:
        return "failed to connect to all addresses"


class _Health:
    """Thay `HealthStub`: gọi như constructor, ghi lại kênh và request."""

    def __init__(self, reply: Any) -> None:
        self.reply = reply
        self.channels: list[_Channel] = []
        self.requests: list[tuple[str, float | None]] = []

    def __call__(self, channel: _Channel) -> _Health:
        self.channels.append(channel)
        return self

    def Check(self, request: Any, timeout: float | None = None) -> Any:
        self.requests.append((request.service, timeout))
        if isinstance(self.reply, Exception):
            raise self.reply
        return health_pb2.HealthCheckResponse(status=self.reply)


def _gateway_settings(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    from jobcrawler.bootstrap import cli
    from jobcrawler.common.config import Settings

    fields: dict[str, Any] = {
        "gateway_host": "0.0.0.0",
        "gateway_port": 50051,
        "gateway_tls_cert": None,
        "gateway_tls_key": None,
    }
    settings = Settings(**(fields | overrides))
    monkeypatch.setattr(cli, "get_settings", lambda: settings)


def _health(monkeypatch: pytest.MonkeyPatch, reply: Any) -> _Health:
    fake = _Health(reply)
    monkeypatch.setattr(health_pb2_grpc, "HealthStub", fake)
    monkeypatch.setattr(grpc, "insecure_channel", lambda target, **kw: _Channel(target, **kw))
    return fake


def test_gateway_health_serving(monkeypatch: pytest.MonkeyPatch) -> None:
    _gateway_settings(monkeypatch)
    fake = _health(monkeypatch, health_pb2.HealthCheckResponse.SERVING)
    result = runner.invoke(app, ["gateway-health"])
    assert result.exit_code == 0, result.output
    assert "SERVING 127.0.0.1:50051" in result.output
    assert fake.channels[0].target == "127.0.0.1:50051"
    assert fake.requests == [("", 5.0)]


@pytest.mark.parametrize(
    ("reply", "message"),
    [
        (health_pb2.HealthCheckResponse.NOT_SERVING, "NOT_SERVING"),
        (_RpcFailure(), "UNAVAILABLE failed to connect to all addresses"),
    ],
)
def test_gateway_health_fails(monkeypatch: pytest.MonkeyPatch, reply: Any, message: str) -> None:
    _gateway_settings(monkeypatch)
    _health(monkeypatch, reply)
    result = runner.invoke(
        app, ["gateway-health", "--addr", "gw:6000", "--service", "x", "--timeout", "1"]
    )
    assert result.exit_code == 1
    assert f"gw:6000: {message}" in result.output


def test_gateway_health_uses_tls_when_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cert, key = tmp_path / "gw.crt", tmp_path / "gw.key"
    cert.write_bytes(b"CERT")
    key.write_bytes(b"KEY")
    _gateway_settings(monkeypatch, gateway_tls_cert=cert, gateway_tls_key=key, gateway_port=6443)
    fake = _health(monkeypatch, health_pb2.HealthCheckResponse.SERVING)
    roots: list[bytes | None] = []

    def credentials(root_certificates: bytes | None = None) -> str:
        roots.append(root_certificates)
        return "creds"

    monkeypatch.setattr(grpc, "ssl_channel_credentials", credentials)
    monkeypatch.setattr(
        grpc, "secure_channel", lambda target, creds, **kw: _Channel(target, creds=creds, **kw)
    )
    result = runner.invoke(app, ["gateway-health", "--server-name", "gw.example.com"])
    assert result.exit_code == 0, result.output
    assert roots == [b"CERT"]
    channel = fake.channels[0]
    assert channel.target == "127.0.0.1:6443"
    assert channel.kwargs == {
        "creds": "creds",
        "options": [("grpc.ssl_target_name_override", "gw.example.com")],
    }


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("0.0.0.0", "127.0.0.1:50051"),
        ("", "127.0.0.1:50051"),
        ("::", "[::1]:50051"),
        ("[::]", "[::1]:50051"),
        ("10.0.0.5", "10.0.0.5:50051"),
        ("fd00::5", "[fd00::5]:50051"),
    ],
)
def test_local_gateway_addr(host: str, expected: str) -> None:
    from jobcrawler.bootstrap.cli import _local_gateway_addr

    assert _local_gateway_addr(host, 50051) == expected
