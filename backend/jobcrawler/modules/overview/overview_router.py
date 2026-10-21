"""API trang Tổng quan: `/api/overview`, `/api/stats`."""

from __future__ import annotations

import dataclasses
from typing import Annotated

from fastapi import APIRouter, Query, Request

from jobcrawler.common.api import res_of
from jobcrawler.modules.overview import overview_service, stats_service
from jobcrawler.modules.overview.overview_schema import OverviewOut, StatsOut

router = APIRouter(prefix="/api", tags=["overview"])


@router.get("/overview")
async def get_overview(
    request: Request,
    hours: Annotated[float, Query(description="Cửa sổ giờ; 0 = tất cả")] = 24,
    storage: bool = False,
) -> OverviewOut:
    """Số liệu + biểu đồ trang tổng quan (services/overview.py: phân bố tính sẵn bằng cron,
    phần theo cửa sổ cache 60 giây)."""
    payload = await overview_service.build_overview(
        res_of(request), hours=hours or None, storage=storage
    )
    return OverviewOut.model_validate(payload)


@router.get("/stats")
async def get_stats(
    request: Request,
    hours: Annotated[float, Query(description="Cửa sổ giờ; 0 = tất cả")] = 24,
    storage: bool = False,
) -> StatsOut:
    st = await stats_service.collect_stats(res_of(request), hours=hours or None, storage=storage)
    return StatsOut.model_validate(dataclasses.asdict(st))
