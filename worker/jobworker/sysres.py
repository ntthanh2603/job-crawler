"""Tài nguyên máy (CPU, RAM — tính cả giới hạn cgroup của container) và số slot tự động của worker.

Như open-asm (`WORKER_MAX_CONCURRENCY=0` = tự động): WORKER_HTTP_SLOTS / WORKER_RENDER_SLOTS để
trống thì tự tính theo máy, giới hạn trên / dưới; đặt số thì dùng số đó.

- HTTP (curl_cffi, chờ mạng là chính): 8 slot mỗi CPU, trong [8, 64].
- Render (Chromium, ~300–500 MB mỗi trang): 1 slot mỗi 768 MiB RAM, không quá số CPU, trong [1, 8].
"""

from __future__ import annotations

import math
import os
from pathlib import Path

RENDER_MEMORY_BYTES = 768 * 1024 * 1024


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def cpu_count() -> int:
    """CPU dùng được: quota cgroup (v2 `cpu.max`, v1 `cfs_quota_us`) nếu có, không thì số CPU."""
    v2 = _read("/sys/fs/cgroup/cpu.max")
    if v2 and not v2.startswith("max"):
        quota, _, period = v2.partition(" ")
        if quota.isdigit() and period.isdigit() and int(period) > 0:
            return max(1, math.ceil(int(quota) / int(period)))
    quota_v1 = _read("/sys/fs/cgroup/cpu/cpu.cfs_quota_us") or ""
    period_v1 = _read("/sys/fs/cgroup/cpu/cpu.cfs_period_us") or ""
    if quota_v1.isdigit() and period_v1.isdigit() and int(quota_v1) > 0 and int(period_v1) > 0:
        return max(1, math.ceil(int(quota_v1) / int(period_v1)))  # quota -1 = không giới hạn
    count = getattr(os, "process_cpu_count", os.cpu_count)()
    return max(1, count or 1)


def memory_bytes() -> int | None:
    """RAM dùng được: giới hạn cgroup nếu có, không thì RAM của máy; None nếu không đọc được."""
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        value = _read(path)
        # "max" / số khổng lồ = không giới hạn.
        if value and value.isdigit() and int(value) < 1 << 60:
            return int(value)
    sysconf = getattr(os, "sysconf", None)  # không có trên Windows
    try:
        return sysconf("SC_PHYS_PAGES") * sysconf("SC_PAGE_SIZE") if sysconf else None
    except (ValueError, OSError):
        return None


def clamp(value: float, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def auto_http_slots(cpus: int | None = None) -> int:
    return clamp(8 * (cpus or cpu_count()), 8, 64)


def auto_render_slots(cpus: int | None = None, memory: int | None = -1) -> int:
    cpus = cpus or cpu_count()
    mem = memory_bytes() if memory == -1 else memory
    by_memory = mem // RENDER_MEMORY_BYTES if mem else 2
    return clamp(min(cpus, by_memory), 1, 8)
