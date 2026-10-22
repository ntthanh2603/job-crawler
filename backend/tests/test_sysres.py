"""`jobcrawler.common.sysres`: số job song song tự động của runner theo CPU / RAM."""

from __future__ import annotations

import pytest

from jobcrawler.bootstrap.runner import concurrency
from jobcrawler.common import sysres
from jobcrawler.common.config import Settings

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
def test_runner_starts_at_machine_level_capped_by_setting() -> None:
    default = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    limits = (
        default.runner_build_concurrency,
        default.runner_poll_concurrency,
        default.runner_board_concurrency,
    )
    assert limits == (25, 60, 4)  # số mặc định, không phải 0
    assert concurrency(default, "poll") == min(60, sysres.auto_concurrency("poll"))
    low = Settings(_env_file=None, runner_poll_concurrency=7)  # pyright: ignore[reportCallIssue]
    assert concurrency(low, "poll") == 7  # mức theo máy ≥ 20 nhưng cài đặt là tối đa


def test_more_worker_slots_allow_more_jobs() -> None:
    """Như open-asm: thông lượng theo tổng năng lực worker. Thêm worker (thêm slot HTTP) là runner
    nhận thêm lượt build / poll, trong trần mỗi process và trần RAM; chưa có worker thì theo máy."""
    mem = 16 * GIB
    machine = sysres.auto_concurrency("build", cpus=12, memory=mem)
    assert machine == 20
    build = {"limit": 25, "processes": 3, "cpus": 12, "memory": mem}
    one = sysres.scaled_concurrency("build", slots=64, **build)
    three = sysres.scaled_concurrency("build", slots=192, **build)
    assert one * 3 >= machine  # không dưới mức theo máy
    assert three > one
    assert three * 3 == 63  # 192 × 0,35 = 67, trần RAM 4 × 16 = 64 → 21 mỗi process × 3
    assert sysres.scaled_concurrency("build", slots=10_000, **build) == 21
    assert sysres.scaled_concurrency("build", slots=10_000, **{**build, "limit": 10}) == 10
    assert sysres.scaled_concurrency("build", slots=0, **build) == 6
    poll = sysres.scaled_concurrency("poll", slots=192, limit=60, processes=3, cpus=12)
    assert poll * 3 == 132  # 192 × 0,7 = 134
    assert sysres.scaled_concurrency("poll", slots=10_000, limit=60, processes=2, cpus=12) == 60
    # Trang tuyển dụng chỉ theo máy, không quá cài đặt.
    assert sysres.scaled_concurrency("boards", slots=192, limit=4, cpus=8) == 4
    assert sysres.scaled_concurrency("boards", slots=192, limit=8, cpus=2) == 2


def test_runner_processes_follow_cpus() -> None:
    assert sysres.runner_processes(2) == (0, 0)
    assert sysres.runner_processes(4) == (1, 2)
    assert sysres.runner_processes(12) == (3, 3)
    assert sysres.runner_processes(64) == (4, 4)
