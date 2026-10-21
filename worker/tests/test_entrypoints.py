"""Điểm vào của worker (`jobworker` CLI, process `jobworker run`) import được trong một tiến trình
Python mới: bắt vòng import chỉ lộ ra theo thứ tự import của process đó."""

from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.mark.parametrize("module", ["jobworker.bootstrap.cli", "jobworker.bootstrap.run"])
def test_entrypoint_imports_in_fresh_interpreter(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
