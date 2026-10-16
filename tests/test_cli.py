from __future__ import annotations

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


def test_infra_subcommands_have_check() -> None:
    for group in ("proxies", "llm"):
        result = runner.invoke(app, [group, "--help"])
        assert result.exit_code == 0
        assert "check" in result.output


def test_db_help_lists_subcommands() -> None:
    result = runner.invoke(app, ["db", "--help"])
    assert result.exit_code == 0
    for command in ("upgrade", "downgrade", "current", "revision"):
        assert command in result.output


@pytest.mark.parametrize("args", [["stats"], ["export"], ["index-boards"], ["import", "x.csv"]])
def test_unimplemented_commands_exit_nonzero(args: list[str]) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == 1
    assert "Chưa cài đặt" in result.output
