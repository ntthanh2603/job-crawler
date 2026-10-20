"""`jobcrawler.sysres`: số job song song tự động của runner theo CPU / RAM."""

from __future__ import annotations

import pytest

from jobcrawler import sysres
from jobcrawler.config import Settings
from jobcrawler.entrypoints.runner import concurrency

GIB = 1024**3


def test_auto_concurrency_by_role_with_bounds() -> None:
    assert sysres.auto_concurrency("poll", cpus=4) == 48
    assert sysres.auto_concurrency("poll", cpus=1) == 20
    assert sysres.auto_concurrency("poll", cpus=64) == 100
    assert sysres.auto_concurrency("build", cpus=4, memory=8 * GIB) == 10
    assert sysres.auto_concurrency("build", cpus=8, memory=3 * GIB) == 6  # RAM ít: theo RAM
    assert sysres.auto_concurrency("build", cpus=1, memory=None) == 4  # sàn
    assert sysres.auto_concurrency("build", cpus=32, memory=None) == 20  # trần
    assert sysres.auto_concurrency("boards", cpus=4) == 4
    assert sysres.auto_concurrency("boards", cpus=1) == 2
    assert sysres.cpu_count() >= 1


@pytest.mark.usefixtures("clean_env")
def test_runner_uses_setting_or_auto() -> None:
    auto = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert concurrency(auto, "poll") == sysres.auto_concurrency("poll")
    fixed = Settings(_env_file=None, runner_poll_concurrency=7)  # pyright: ignore[reportCallIssue]
    assert concurrency(fixed, "poll") == 7
