"""Mô hình vào / ra của API proxy."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from jobcrawler.common.api import ApiOut
from jobcrawler.modules.proxies import proxies_service


class ProxyStatusOut(ApiOut):
    """Một proxy chung và trạng thái xoay vòng của nó (`services.proxies.ProxyStatus`)."""

    id: str
    line: str  # đã che mật khẩu, khớp một dòng của cài đặt `proxies`
    source: str  # "settings" | "file"
    scheme: str
    host: str
    port: int
    username: str | None
    has_password: bool
    score: int
    fails: int
    dead: bool
    dead_reason: str | None
    dead_seconds: int | None
    cooling_domains: int
    sticky_domains: int


class ProxiesOut(ApiOut):
    # Chế độ đang dùng: chỉ "central" lấy proxy từ danh sách này.
    mode: Literal["worker", "central", "off"]
    check_url: str
    items: list[ProxyStatusOut]


class ProxyCheckIn(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=proxies_service.CHECK_BATCH)


class ProxyCheckOut(ApiOut):
    """Kết quả gọi thử qua một proxy (`services.health.ProxyCheck`)."""

    proxy_id: str
    proxy: str  # đã che mật khẩu
    ok: bool
    status: int | None
    latency_ms: int | None
    exit_ip: str | None
    location: str | None
    error: str | None


class ProxyChecksOut(ApiOut):
    results: list[ProxyCheckOut]
