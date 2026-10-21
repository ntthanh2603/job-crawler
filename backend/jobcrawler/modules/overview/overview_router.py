"""API trang Tổng quan: `/api/overview`, `/api/stats`."""

from __future__ import annotations

import dataclasses
from typing import Annotated

from fastapi import APIRouter, Query, Request

from jobcrawler.common.api import MAX_HOURS, res_of
from jobcrawler.modules.overview import overview_service, stats_service
from jobcrawler.modules.overview.overview_schema import OverviewOut, StatsOut

router = APIRouter(prefix="/api", tags=["overview"])

# Cửa sổ thống kê (giờ): số hữu hạn trong [0, MAX_HOURS].
Hours = Annotated[
    float,
    Query(ge=0, le=MAX_HOURS, allow_inf_nan=False, description="Cửa sổ giờ; 0 = tất cả"),
]


@router.get("/overview")
async def get_overview(
    request: Request,
    hours: Hours = 24,
    storage: bool = False,
) -> OverviewOut:
    """Số liệu + biểu đồ trang tổng quan (`overview_service`: phân bố tính sẵn bằng cron,
    phần theo cửa sổ cache 60 giây)."""
    payload = await overview_service.build_overview(
        res_of(request), hours=hours or None, storage=storage
    )
    return OverviewOut.model_validate(payload)


@router.get("/stats")
async def get_stats(
    request: Request,
    hours: Hours = 24,
    storage: bool = False,
) -> StatsOut:
    st = await stats_service.collect_stats(res_of(request), hours=hours or None, storage=storage)
    return StatsOut.model_validate(dataclasses.asdict(st))
