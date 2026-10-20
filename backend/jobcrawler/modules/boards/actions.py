"""Thao tác trang tuyển dụng cho API: danh sách trang, xếp lượt index."""

from __future__ import annotations

from jobcrawler.bootstrap.runner import board_job_timeout
from jobcrawler.services.resources import Resources
from jobcrawler.sources import boards


def board_names() -> list[str]:
    return list(boards.registry())


async def enqueue_board(res: Resources, name: str) -> bool:
    """Đưa một trang tuyển dụng vào queue index (runner vai `boards`). False nếu trang đó đang
    chờ/đang index."""
    if name not in boards.registry():
        raise KeyError(name)
    job = await res.queues.boards.enqueue(
        "index_board",
        source=name,
        key=f"board:{name}",
        timeout=board_job_timeout(res.settings),
    )
    return job is not None
