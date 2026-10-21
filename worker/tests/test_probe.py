"""`jobworker check` (`modules/health/probe_service.py`): kết nối gateway, IP đi ra qua từng
proxy."""

from __future__ import annotations

from worker_fakes import (  # pyright: ignore[reportMissingImports]
    FakeGateway,
    ForwardProxy,
    WebServer,
    closed_port,
    make_settings,
)

from jobworker.modules.health.probe_service import DIRECT_LABEL, check_gateway, check_routes
from jobworker.services.proxies import parse_proxy_line


async def test_gateway_reachable(gateway: tuple[FakeGateway, int]) -> None:
    _, port = gateway
    result = await check_gateway(make_settings(port), timeout=5)
    assert result.ok
    assert result.ms >= 0


async def test_gateway_unreachable() -> None:
    result = await check_gateway(make_settings(closed_port()), timeout=1)
    assert not result.ok


async def test_routes_direct_and_each_proxy(web: WebServer) -> None:
    good = ForwardProxy("u", "s3cret")
    dead = f"127.0.0.1:{closed_port()}"
    try:
        settings = make_settings(1, worker_proxies=[good.url, dead])
        direct, through, failed = await check_routes(settings, timeout=5, url=web.url("/trace"))
        assert good.requests == [web.url("/trace")]
    finally:
        good.close()

    assert (direct.proxy_id, direct.label, direct.error) == ("", DIRECT_LABEL, "")
    assert (direct.ip, direct.loc) == ("127.0.0.1", "VN")

    proxy = parse_proxy_line(good.url)
    assert proxy is not None
    assert through.proxy_id == proxy.id
    assert through.label == f"{proxy.id} {proxy.display}"
    assert "s3cret" not in through.label  # mật khẩu đã che
    assert (through.error, through.ip, through.loc) == ("", "127.0.0.1", "VN")

    assert failed.error
    assert failed.ip == ""


async def test_routes_report_http_status(web: WebServer) -> None:
    (direct,) = await check_routes(make_settings(1), timeout=5, url=web.url("/403"))
    assert direct.error == "HTTP 403"
