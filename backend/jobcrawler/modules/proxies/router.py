"""API proxy chung trong trang Cài đặt (`/api/proxies…`): trạng thái xoay vòng và kiểm tra qua
worker (`modules.proxies.proxies`)."""

from __future__ import annotations

import dataclasses

from fastapi import APIRouter, Request

from jobcrawler.common.api import actor_of, res_of, settings_of
from jobcrawler.common.logging import get_logger
from jobcrawler.modules.proxies import proxies as proxy_service
from jobcrawler.modules.proxies.schemas import ProxiesOut, ProxyCheckIn, ProxyChecksOut
from jobcrawler.modules.settings import app_settings

router = APIRouter(prefix="/api", tags=["settings"])

log = get_logger(__name__)


@router.get("/proxies")
async def get_proxies(request: Request) -> ProxiesOut:
    """Danh sách proxy chung (cài đặt `proxies` rồi PROXIES_FILE) kèm trạng thái xoay vòng."""
    res, base = res_of(request), settings_of(request)
    settings = await app_settings.load_effective(res.sessionmaker, base)
    items = await proxy_service.statuses(res.redis, settings)
    return ProxiesOut.model_validate(
        {
            "mode": settings.proxy_mode,
            "check_url": settings.proxy_check_url,
            "items": [dataclasses.asdict(i) for i in items],
        }
    )


@router.post("/proxies/check")
async def check_proxies(request: Request, body: ProxyCheckIn) -> ProxyChecksOut:
    """Gọi trang kiểm tra qua từng proxy trong `ids` (tối đa 20 mỗi lần) từ worker. Proxy sống
    được đưa lại vào vòng xoay, proxy lỗi bị loại. Không có worker thì báo lỗi, không loại proxy."""
    res, base = res_of(request), settings_of(request)
    settings = await app_settings.load_effective(res.sessionmaker, base)
    results = await proxy_service.check(res, settings, body.ids)
    log.info(
        "proxies.checked",
        user=actor_of(request),
        total=len(results),
        ok=sum(r.ok for r in results),
    )
    return ProxyChecksOut.model_validate({"results": [dataclasses.asdict(r) for r in results]})
