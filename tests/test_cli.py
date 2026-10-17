from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from jobcrawler.entrypoints.cli import app

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
    from jobcrawler.config import get_settings

    monkeypatch.delenv("PROXY_ENABLED", raising=False)  # để monkeypatch dọn lại sau test
    get_settings.cache_clear()
    try:
        result = runner.invoke(app, ["--no-proxy", "import", str(tmp_path / "x.csv")])
        assert result.exit_code == 2  # file không có; chỉ cần callback đã chạy
        assert get_settings().proxy_enabled is False
    finally:
        get_settings.cache_clear()


def test_index_boards_rejects_unknown_source() -> None:
    result = runner.invoke(app, ["index-boards", "--source", "khong-co"])
    assert result.exit_code == 2
    assert "không có adapter khong-co" in result.output
