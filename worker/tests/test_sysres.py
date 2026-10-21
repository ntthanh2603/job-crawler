"""`jobworker.common.sysres`: số slot tự động theo CPU / RAM (như open-asm)."""

from __future__ import annotations

from jobworker.common import sysres
from jobworker.common.config import WorkerSettings

GIB = 1024**3


def test_auto_slots_follow_machine_with_bounds() -> None:
    assert sysres.auto_http_slots(cpus=1) == 8
    assert sysres.auto_http_slots(cpus=4) == 32
    assert sysres.auto_http_slots(cpus=32) == 64
    assert sysres.auto_render_slots(cpus=8, memory=2 * GIB) == 2  # RAM ít: theo RAM
    assert sysres.auto_render_slots(cpus=2, memory=64 * GIB) == 2  # không quá số CPU
    assert sysres.auto_render_slots(cpus=64, memory=64 * GIB) == 8  # trần
    assert sysres.auto_render_slots(cpus=4, memory=256 * 1024**2) == 1  # sàn
    assert sysres.auto_render_slots(cpus=4, memory=None) == 2  # không đọc được RAM


def test_settings_auto_or_explicit() -> None:
    auto = WorkerSettings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert auto.http_slots == sysres.auto_http_slots()
    assert auto.render_slots == sysres.auto_render_slots()
    fixed = WorkerSettings(_env_file=None, worker_http_slots=5, worker_render_slots=0)  # pyright: ignore[reportCallIssue]
    assert (fixed.http_slots, fixed.render_slots) == (5, 0)
    assert sysres.cpu_count() >= 1
