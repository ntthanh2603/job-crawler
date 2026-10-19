"""Adapter các trang tuyển dụng (mỗi trang một file), dùng cho lượt index tăng dần."""

from __future__ import annotations

from jobcrawler.sources.boards.base import BoardAdapter
from jobcrawler.sources.boards.careerlink import CareerLink
from jobcrawler.sources.boards.careerviet import CareerViet
from jobcrawler.sources.boards.glints import Glints
from jobcrawler.sources.boards.itviec import ITviec
from jobcrawler.sources.boards.job123 import Job123
from jobcrawler.sources.boards.jobsgo import JobsGO
from jobcrawler.sources.boards.topcv import TopCV
from jobcrawler.sources.boards.topdev import TopDev
from jobcrawler.sources.boards.vieclam24h import Vieclam24h
from jobcrawler.sources.boards.vietnamworks import VietnamWorks

# Theo độ phủ (số tin đang mở khi khảo sát 10/2026).
ADAPTERS: tuple[type[BoardAdapter], ...] = (
    TopCV,  # ~45 nghìn
    CareerLink,  # ~34 nghìn
    JobsGO,  # ~28 nghìn (sitemap)
    CareerViet,  # ~19,5 nghìn
    VietnamWorks,  # ~9,8 nghìn
    Glints,  # ~5,7 nghìn
    TopDev,  # ~2,6 nghìn
    Vieclam24h,  # chỉ xem được ~1 nghìn tin mới nhất
    ITviec,  # ~650 (IT)
    Job123,  # RSS 100 tin + vài trang danh sách
)


def registry() -> dict[str, BoardAdapter]:
    """Tên nguồn → adapter, theo thứ tự ưu tiên độ phủ."""
    return {cls.name: cls() for cls in ADAPTERS}


def get(name: str) -> BoardAdapter | None:
    return registry().get(name)
