"""Tài nguyên máy (CPU, RAM — tính cả giới hạn cgroup của container) và số job song song tự động
của runner (như WORKER_MAX_CONCURRENCY=0 của open-asm; worker có bản riêng
`jobworker/common/sysres.py`).

Mức theo máy chạy runner (`auto_concurrency`) là mức sàn. Một process runner là một event loop
Python (tối đa ~1 core cho phần parse), phần lớn thời gian chờ worker / mạng / LLM nên số job vượt
số CPU nhiều lần:

- poll (lượt nhẹ, ~140 ms CPU mỗi lượt): 12 mỗi CPU, trong [20, 100];
- build (nhiều trang, LLM, nặng RAM): 2,5 mỗi CPU, không quá 2 mỗi GiB RAM, trong [4, 20];
- boards (index trang tuyển dụng): 1 mỗi CPU, trong [2, 8].

Máy 4 CPU / 8 GiB cho 48 / 10 / 4 — gần mặc định cũ 50 / 10 / 4.

Runner build / poll chạy thật theo `scaled_concurrency`: theo tổng slot HTTP của worker đang kết
nối (0,35 lượt build / 0,7 lượt poll mỗi slot), không quá RUNNER_*_CONCURRENCY mỗi process (mặc
định 25 build / 60 poll / 4 trang tuyển dụng) và 4 build mỗi GiB RAM; runner đọc lại mỗi
`RESIZE_SECONDS` nên thêm worker là chạy được nhiều lượt hơn ngay (như open-asm: thông lượng là tổng
năng lực của các worker).
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
    """Số job song song tự động của một vai runner trên máy này (chưa tính worker)."""
    cpus = cpus or cpu_count()
    mem = memory_bytes() if memory == -1 else memory
    if role == "poll":
        return _clamp(12 * cpus, 20, 100)
    if role == "build":
        by_cpu = 2.5 * cpus
        return _clamp(min(by_cpu, 2 * mem / GIB) if mem else by_cpu, 4, 20)
    return _clamp(cpus, 2, 8)


# Theo năng lực worker, như open-asm (thông lượng = tổng slot của các worker đang kết nối): runner
# chỉ điều phối, việc lấy trang nằm ở worker, nên số lượt chạy cùng lúc đi theo tổng slot HTTP của
# worker. Một lượt build hay poll giữ trung bình khoảng một request đang chạy ở worker (đo 10/2026:
# 20 lượt build giữ 14–18 request); chừng này lượt mỗi slot để worker còn dư cho vai kia và lúc
# đỉnh.
BUILD_PER_SLOT = 0.35
POLL_PER_SLOT = 0.7
# Trần mỗi process runner là RUNNER_*_CONCURRENCY (mặc định 25 build / 60 poll): một event loop
# Python, phần parse / chuẩn hoá tối đa ~1 core (đo 10/2026: 20 lượt build dùng ~85% một core). Thêm
# lượt nữa thì cần thêm process (`serve.runner_processes`).
# RAM: mỗi lượt build giữ trang và tin trong bộ nhớ (~100 MB lúc đỉnh).
BUILD_PER_GIB = 4


# Từ chừng này CPU trở lên, mỗi vai runner một process; mỗi CPUS_PER_RUNNER CPU thêm một process
# build và một process poll (poll ít nhất POLL_PROCESSES), tối đa MAX_RUNNER_PROCESSES mỗi vai.
SPLIT_RUNNER_CPUS = 4
POLL_PROCESSES = 2
CPUS_PER_RUNNER = 4
MAX_RUNNER_PROCESSES = 4


def runner_processes(cpus: int | None = None) -> tuple[int, int]:
    """(số process build, số process poll) `jobcrawler serve` chạy trên máy `cpus` CPU; (0, 0): máy
    nhỏ, một process `runner` đủ ba vai. Mỗi process một event loop Python, phần parse / chuẩn hoá
    tối đa ~1 core, nên nhiều lượt chạy cùng lúc (nhiều worker) cần nhiều process."""
    cpus = cpus or cpu_count()
    if cpus < SPLIT_RUNNER_CPUS:
        return 0, 0
    n = max(1, min(MAX_RUNNER_PROCESSES, cpus // CPUS_PER_RUNNER))
    return n, max(POLL_PROCESSES, n)


def scaled_concurrency(
    role: Role,
    *,
    slots: int,
    limit: int,
    processes: int = 1,
    cpus: int | None = None,
    memory: int | None = -1,
) -> int:
    """Số job song song của MỖI process trong `processes` process cùng chạy vai `role`, khi worker
    đang kết nối có tổng `slots` slot HTTP: thêm worker là chạy được nhiều lượt hơn. Không dưới mức
    theo máy (`auto_concurrency`, cũng là mức khi chưa có worker nào), không quá `limit` mỗi process
    (RUNNER_*_CONCURRENCY) và trần RAM. Vai `boards` chỉ theo máy (vài trang tuyển dụng, mỗi trang
    một lượt)."""
    machine = auto_concurrency(role, cpus=cpus, memory=memory)
    processes = max(1, processes)
    total = float(machine)
    if role != "boards" and slots > 0:
        total = max(total, slots * (BUILD_PER_SLOT if role == "build" else POLL_PER_SLOT))
    if role == "build":
        mem = memory_bytes() if memory == -1 else memory
        if mem:
            total = min(total, BUILD_PER_GIB * mem / GIB)
    return max(1, min(limit, int(total) // processes))
