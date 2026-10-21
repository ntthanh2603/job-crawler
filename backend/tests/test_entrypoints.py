"""Mỗi điểm vào process (`jobcrawler serve` chạy api / gateway / scheduler / runner thành process
con) import được trong một tiến trình Python mới: bắt vòng import chỉ lộ ra theo thứ tự import của
process đó (test khác đã import sẵn module thì không thấy)."""

from __future__ import annotations

import subprocess
import sys

import pytest

ENTRYPOINTS = [
    "jobcrawler.bootstrap.cli",
    "jobcrawler.bootstrap.app",
    "jobcrawler.bootstrap.gateway",
    "jobcrawler.bootstrap.runner",
    "jobcrawler.bootstrap.scheduler",
    "jobcrawler.bootstrap.serve",
]


@pytest.mark.parametrize("module", ENTRYPOINTS)
def test_entrypoint_imports_in_fresh_interpreter(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
