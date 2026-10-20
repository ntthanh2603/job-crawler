"""Tài nguyên máy (CPU, RAM — tính cả giới hạn cgroup của container) và số job song song tự động
của runner (như WORKER_MAX_CONCURRENCY=0 của open-asm; worker có bản riêng `jobworker/sysres.py`).

RUNNER_*_CONCURRENCY = 0 (mặc định) thì tự tính theo máy chạy runner, giới hạn trên / dưới; đặt số
thì dùng số đó. Một process runner là một event loop Python (tối đa ~1 core cho phần parse), phần
lớn thời gian chờ worker / mạng / LLM nên số job vượt số CPU nhiều lần:

- poll (lượt nhẹ, ~140 ms CPU mỗi lượt): 12 mỗi CPU, trong [20, 100];
- build (nhiều trang, LLM, nặng RAM): 2,5 mỗi CPU, không quá 2 mỗi GiB RAM, trong [4, 20];
- boards (index trang tuyển dụng): 1 mỗi CPU, trong [2, 8].

Máy 4 CPU / 8 GiB cho 48 / 10 / 4 — gần mặc định cũ 50 / 10 / 4.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Literal

Role = Literal["build", "poll", "boards"]
GIB = 1024**3


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def cpu_count() -> int:
    """CPU dùng được: quota cgroup (v2 `cpu.max`, v1 `cfs_quota_us`) nếu có, không thì số CPU."""
    quota, _, period = _read("/sys/fs/cgroup/cpu.max").partition(" ")
    if quota.isdigit() and period.isdigit() and int(period) > 0:
        return max(1, math.ceil(int(quota) / int(period)))
    quota_v1 = _read("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
    period_v1 = _read("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
    if quota_v1.isdigit() and period_v1.isdigit() and int(quota_v1) > 0 and int(period_v1) > 0:
        return max(1, math.ceil(int(quota_v1) / int(period_v1)))  # quota -1 = không giới hạn
    count = getattr(os, "process_cpu_count", os.cpu_count)()
    return max(1, count or 1)


def memory_bytes() -> int | None:
    """RAM dùng được: giới hạn cgroup nếu có, không thì RAM của máy; None nếu không đọc được."""
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        value = _read(path)
        # "max" / số khổng lồ = không giới hạn.
        if value.isdigit() and int(value) < 1 << 60:
            return int(value)
    sysconf = getattr(os, "sysconf", None)  # không có trên Windows
    try:
        return sysconf("SC_PHYS_PAGES") * sysconf("SC_PAGE_SIZE") if sysconf else None
    except (ValueError, OSError):
        return None


def _clamp(value: float, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def auto_concurrency(role: Role, *, cpus: int | None = None, memory: int | None = -1) -> int:
    """Số job song song tự động của một vai runner trên máy này."""
    cpus = cpus or cpu_count()
    mem = memory_bytes() if memory == -1 else memory
    if role == "poll":
        return _clamp(12 * cpus, 20, 100)
    if role == "build":
        by_cpu = 2.5 * cpus
        return _clamp(min(by_cpu, 2 * mem / GIB) if mem else by_cpu, 4, 20)
    return _clamp(cpus, 2, 8)
