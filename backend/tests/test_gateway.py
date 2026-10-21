"""Gateway / hàng việc lấy trang, phần không cần Redis: mã hoá request, đọc kết quả của worker,
định tuyến, cấu hình worker, task_id, token, mức ưu tiên, khoá Redis."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import pytest
import zstandard

from jcproto.v1 import crawl_pb2
from jobcrawler.common.config import Settings
from jobcrawler.modules.gateway import remote_service
from jobcrawler.modules.gateway.dispatch_service import (
    NoWorkerError,
    choose_pool,
    fetch_priority,
    make_client_id,
    match_route,
    parse_task_id,
    priority_fetches,
)
from jobcrawler.modules.gateway.registry_service import Keys
from jobcrawler.modules.gateway.remote_service import (
    RemoteRenderer,
    RemoteTransport,
    encode_body,
    proxy_id_of,
    to_raw_response,
)
from jobcrawler.modules.gateway.server_service import (
    client_ip,
    config_version,
    peer_ip,
    worker_config,
)
from jobcrawler.modules.gateway.worker_auth_service import bearer
from jobcrawler.services.net.http import CapturedJson, TransportError
from jobcrawler.services.net.proxy_pool import parse_proxy_line

URL = "https://careers.example.vn/jobs"


# --- body request --------------------------------------------------------------------------------


def test_encode_json_body_is_compact_utf8_with_content_type() -> None:
    headers, body = encode_body({"X-A": "1"}, json={"q": "Kế toán", "page": 2})
    assert body == '{"q":"Kế toán","page":2}'.encode()
    assert headers == {"X-A": "1", "Content-Type": "application/json"}


def test_encode_keeps_given_content_type() -> None:
    headers, body = encode_body({"content-type": "application/vnd.api+json"}, json=[1])
    assert headers == {"content-type": "application/vnd.api+json"}
    assert body == b"[1]"


@pytest.mark.parametrize(
    "data", [{"a": "1", "b": ["x y", "z"]}, [("a", "1"), ("b", "x y"), ("b", "z")]]
)
def test_encode_form_body(data: Any) -> None:
    headers, body = encode_body({}, data=data)
    assert body == b"a=1&b=x+y&b=z"
    assert headers == {"Content-Type": "application/x-www-form-urlencoded"}


@pytest.mark.parametrize(("data", "body"), [(b"\x00raw", b"\x00raw"), ("chữ", "chữ".encode())])
def test_encode_raw_body_without_content_type(data: Any, body: bytes) -> None:
    assert encode_body({}, data=data) == ({}, body)


def test_encode_no_body() -> None:
    assert encode_body({"A": "1"}) == ({"A": "1"}, b"")


# --- kết quả của worker → RawResponse ------------------------------------------------------------


def test_result_maps_to_raw_response() -> None:
    result = crawl_pb2.FetchResult(
        task_id="h-1-ab.1",
        status=200,
        final_url=f"{URL}?page=1",
        headers={"Content-Type": "text/html", "x-a": "1"},
        body=b"<p>ok</p>",
        proxy_id="w:ab12",
        worker_id="w1",
    )
    raw = to_raw_response(result, URL)
    assert (raw.status, raw.url, raw.content, raw.proxy_id) == (
        200,
        f"{URL}?page=1",
        b"<p>ok</p>",
        "w:ab12",
    )
    assert dict(raw.headers) == {"content-type": "text/html", "x-a": "1"}
    assert raw.captured == ()


def test_result_defaults_final_url_and_proxy_id() -> None:
    raw = to_raw_response(crawl_pb2.FetchResult(status=404), URL)
    assert (raw.status, raw.url, raw.proxy_id, raw.content) == (404, URL, None, b"")


@pytest.mark.parametrize("content_size", [True, False])
def test_result_body_zstd(content_size: bool) -> None:
    html = "<html>Tuyển dụng</html>".encode() * 100
    body = zstandard.ZstdCompressor(write_content_size=content_size).compress(html)
    raw = to_raw_response(crawl_pb2.FetchResult(status=200, body=body, body_zstd=True), URL)
    assert raw.content == html


def test_result_zstd_larger_than_limit_is_rejected() -> None:
    body = zstandard.ZstdCompressor().compress(b"x" * 10_000)
    with pytest.raises(TransportError) as info:
        to_raw_response(
            crawl_pb2.FetchResult(status=200, body=body, body_zstd=True), URL, max_body=1000
        )
    assert info.value.kind == "other"
    assert "too_large" in str(info.value)
    with pytest.raises(TransportError, match="zstd"):
        to_raw_response(crawl_pb2.FetchResult(status=200, body=b"nope", body_zstd=True), URL)


def test_result_captured_json() -> None:
    result = crawl_pb2.FetchResult(
        status=200,
        captured=[
            crawl_pb2.CapturedJson(
                url=f"{URL}/api", method="POST", status=200, body=b"{}", request_json='{"q":"a"}'
            ),
            crawl_pb2.CapturedJson(url=f"{URL}/api2", status=200, body=b"[]", request_json="{x"),
        ],
    )
    raw = to_raw_response(result, URL)
    assert raw.captured == (
        CapturedJson(
            url=f"{URL}/api", method="POST", status=200, body=b"{}", request_json={"q": "a"}
        ),
        CapturedJson(url=f"{URL}/api2", method="GET", status=200, body=b"[]", request_json="{x"),
    )


@pytest.mark.parametrize(
    ("error_kind", "kind", "text"),
    [
        (crawl_pb2.ERROR_KIND_TIMEOUT, "timeout", "boom"),
        (crawl_pb2.ERROR_KIND_PROXY, "proxy", "boom"),
        (crawl_pb2.ERROR_KIND_CONNECT, "connect", "boom"),
        (crawl_pb2.ERROR_KIND_OTHER, "other", "boom"),
        (crawl_pb2.ERROR_KIND_EXPIRED, "other", "expired: boom"),
        (crawl_pb2.ERROR_KIND_TOO_LARGE, "other", "too_large: boom"),
        (crawl_pb2.ERROR_KIND_UNSUPPORTED, "other", "unsupported: boom"),
        (crawl_pb2.ERROR_KIND_WORKER_LOST, "other", "worker_lost: boom"),
    ],
)
def test_result_error_kinds(error_kind: int, kind: str, text: str) -> None:
    result = crawl_pb2.FetchResult(error_kind=cast(Any, error_kind), error="boom", worker_id="w1")
    with pytest.raises(TransportError) as info:
        to_raw_response(result, URL)
    assert info.value.kind == kind
    assert str(info.value) == f"{text} (worker w1)"


def test_result_without_status_is_error() -> None:
    with pytest.raises(TransportError) as info:
        to_raw_response(crawl_pb2.FetchResult(), URL)
    assert info.value.kind == "other"
    with pytest.raises(TransportError, match="expired"):
        to_raw_response(crawl_pb2.FetchResult(error_kind=crawl_pb2.ERROR_KIND_EXPIRED), URL)


# --- RemoteTransport / RemoteRenderer dựng FetchTask ---------------------------------------------


class FakeClient:
    max_body_bytes = 10 * 1024 * 1024

    def __init__(self, *results: crawl_pb2.FetchResult) -> None:
        self.tasks: list[crawl_pb2.FetchTask] = []
        self.results = list(results) or [crawl_pb2.FetchResult(status=200, body=b"ok")]

    async def submit(self, task: crawl_pb2.FetchTask) -> crawl_pb2.FetchResult:
        self.tasks.append(crawl_pb2.FetchTask.FromString(task.SerializeToString()))
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


async def test_remote_transport_builds_http_task() -> None:
    client = FakeClient()
    transport = RemoteTransport(cast(Any, client), direct=True)
    raw = await transport.request(
        "post",
        URL,
        headers={"Accept-Language": "vi"},
        json={"a": 1},
        proxy=None,
        timeout=7.5,
        allow_redirects=False,
    )
    assert (raw.status, raw.content) == (200, b"ok")
    [task] = client.tasks
    assert task.kind == crawl_pb2.TASK_KIND_HTTP
    assert (task.method, task.url, task.body) == ("POST", URL, b'{"a":1}')
    assert dict(task.headers) == {"Accept-Language": "vi", "Content-Type": "application/json"}
    assert (task.timeout_seconds, task.allow_redirects) == (7.5, False)
    assert (task.proxy, task.proxy_id, task.direct) == ("", "", True)


async def test_remote_transport_passes_backend_proxy() -> None:
    client = FakeClient()
    proxy = "http://u:p@1.2.3.4:8080"
    await RemoteTransport(cast(Any, client), direct=True).request(
        "GET", URL, headers={}, proxy=proxy, timeout=5, allow_redirects=True
    )
    [task] = client.tasks
    expected = parse_proxy_line(proxy)
    assert expected is not None
    # Proxy chỉ định (kiểm tra proxy) thắng `direct`.
    assert (task.proxy, task.proxy_id, task.direct) == (proxy, expected.id, False)
    assert proxy_id_of("not a proxy") == ""


async def test_remote_renderer_builds_render_task() -> None:
    proxy = parse_proxy_line("http://5.6.7.8:3128")
    client = FakeClient(crawl_pb2.FetchResult(status=200, body=b"<div/>", proxy_id="x"))
    raw = await RemoteRenderer(cast(Any, client)).render(
        URL, proxy=proxy, timeout=40, wait_until="networkidle", capture_json=True
    )
    assert raw.content == b"<div/>"
    [task] = client.tasks
    assert task.kind == crawl_pb2.TASK_KIND_RENDER
    assert (task.method, task.wait_until, task.capture_json, task.timeout_seconds) == (
        "GET",
        "networkidle",
        True,
        40,
    )
    assert proxy is not None
    assert (task.proxy, task.proxy_id, task.direct) == (proxy.url, proxy.id, False)


async def test_remote_transport_raises_worker_errors() -> None:
    client = FakeClient(crawl_pb2.FetchResult(error_kind=crawl_pb2.ERROR_KIND_PROXY, error="407"))
    with pytest.raises(TransportError) as info:
        await RemoteTransport(cast(Any, client)).request(
            "GET", URL, headers={}, proxy=None, timeout=5, allow_redirects=True
        )
    assert info.value.kind == "proxy"
    assert info.value.proxy_fault


LOST = crawl_pb2.FetchResult(
    error_kind=crawl_pb2.ERROR_KIND_WORKER_LOST, error="worker đã rời", worker_id="w1"
)


async def test_remote_resubmits_once_when_worker_is_lost(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(remote_service, "LOST_RESUBMIT_DELAY_SECONDS", 0)
    client = FakeClient(LOST, crawl_pb2.FetchResult(status=200, body=b"<div/>"))
    raw = await RemoteRenderer(cast(Any, client)).render(
        URL, proxy=None, timeout=30, wait_until="load"
    )
    assert raw.content == b"<div/>"
    assert [t.url for t in client.tasks] == [URL, URL]

    # Mất worker lần nữa: lỗi "other" (HttpClient.fetch thử lại), không tính lỗi cho proxy.
    client = FakeClient(LOST)
    with pytest.raises(TransportError) as info:
        await RemoteTransport(cast(Any, client)).request(
            "GET", URL, headers={}, proxy=None, timeout=5, allow_redirects=True
        )
    assert len(client.tasks) == 2
    assert info.value.kind == "other"
    assert str(info.value).startswith("worker_lost:")
    assert not info.value.proxy_fault


# --- mức ưu tiên, khoá Redis ---------------------------------------------------------------------


async def test_priority_fetches_scope_is_inherited_by_child_tasks() -> None:
    assert fetch_priority.get() == 0
    with priority_fetches():
        assert fetch_priority.get() == 1
        child = asyncio.ensure_future(asyncio.sleep(0, result=fetch_priority.get()))
        with priority_fetches(2):
            assert fetch_priority.get() == 2
        assert fetch_priority.get() == 1
    assert fetch_priority.get() == 0
    assert await child == 1
    with pytest.raises(RuntimeError), priority_fetches():
        raise RuntimeError
    assert fetch_priority.get() == 0


def test_stream_keys() -> None:
    keys = Keys("jc")
    assert keys.stream("default", "http") == "jc:gw:fetch:default:http"
    assert keys.stream("vn", "render", high=True) == "jc:gw:fetch:vn:render:hi"
    assert keys.streams("vn", "http") == ("jc:gw:fetch:vn:http:hi", "jc:gw:fetch:vn:http")
    assert keys.inflight("w-1") == "jc:gw:inflight:w-1"
    assert keys.progress("vn", "http") == "jc:gw:progress:vn:http"


# --- định tuyến ----------------------------------------------------------------------------------


ROUTES = {"topcv.vn": "residential", "api.topcv.vn": "datacenter", ".itviec.com": "vn"}


@pytest.mark.parametrize(
    ("host", "pool"),
    [
        ("topcv.vn", "residential"),
        ("www.topcv.vn", "residential"),
        ("api.topcv.vn", "datacenter"),  # hậu tố dài nhất thắng
        ("v2.api.topcv.vn", "datacenter"),
        ("WWW.ITVIEC.COM.", "vn"),
        ("nottopcv.vn", None),
        ("topcv.vn.evil.com", None),
    ],
)
def test_match_route(host: str, pool: str | None) -> None:
    assert match_route(host, ROUTES) == pool


def test_choose_pool_uses_route_then_default() -> None:
    live = {"residential": {"http": 2, "render": 0}, "default": {"http": 1, "render": 1}}
    assert choose_pool("www.topcv.vn", "http", ROUTES, live) == "residential"
    # Pool theo route không có worker render → về default.
    assert choose_pool("www.topcv.vn", "render", ROUTES, live) == "default"
    assert choose_pool("vng.com.vn", "http", ROUTES, live) == "default"
    assert choose_pool("api.topcv.vn", "http", ROUTES, live) == "default"


def test_choose_pool_without_any_worker_fails_fast() -> None:
    with pytest.raises(NoWorkerError) as info:
        choose_pool("www.topcv.vn", "http", ROUTES, {"other": {"http": 3, "render": 3}})
    assert info.value.kind == "other"
    assert str(info.value).startswith("no_worker:")
    assert "residential" in str(info.value)
    with pytest.raises(NoWorkerError):
        choose_pool("a.vn", "render", {}, {"default": {"http": 1, "render": 0}})


# --- task_id / client_id -------------------------------------------------------------------------


def test_client_id_round_trips_through_task_id() -> None:
    client_id = make_client_id()
    assert "." not in client_id
    assert parse_task_id(f"{client_id}.42") == client_id
    assert make_client_id() != client_id


@pytest.mark.parametrize(
    "task_id", ["", "abc", "abc.", ".1", "a.b.1", "abc.x1", "a:b.1", "abc.١", "a" * 129 + ".1"]
)
def test_parse_task_id_rejects_malformed(task_id: str) -> None:
    assert parse_task_id(task_id) is None


# --- cấu hình worker -----------------------------------------------------------------------------


def _settings(**kw: Any) -> Settings:
    return Settings(_env_file=None, **kw)  # pyright: ignore[reportCallIssue]


def test_worker_config_from_settings() -> None:
    config = worker_config(
        _settings(
            gateway_http_slots=32,
            gateway_render_slots=2,
            gateway_impersonate="chrome131",
            fetch_max_body_bytes=2048,
            gateway_heartbeat_seconds=7,
            gateway_lease_wait_ms=900,
            proxy_mode="worker",
        )
    )
    assert (config.http_slots, config.render_slots, config.impersonate) == (32, 2, "chrome131")
    assert (config.max_body_bytes, config.heartbeat_seconds, config.lease_wait_ms) == (
        2048,
        7,
        900,
    )
    assert config.use_local_proxies
    assert not worker_config(_settings(proxy_mode="central")).use_local_proxies
    assert not worker_config(_settings(proxy_mode="off")).use_local_proxies


def test_worker_config_carries_the_shared_proxy_list() -> None:
    """PROXY_MODE=worker: danh sách proxy chung đi xuống worker; đổi danh sách thì đổi version."""
    lines = ["http://u:p@10.0.0.1:8080", "10.0.0.2:3128", "10.0.0.2:3128"]
    a = worker_config(_settings(proxy_mode="worker", proxies=lines))
    assert list(a.shared_proxies) == ["http://u:p@10.0.0.1:8080", "http://10.0.0.2:3128"]
    assert len(a.shared_proxies_hash) == 16
    same = worker_config(_settings(proxy_mode="worker", proxies=list(reversed(lines))))
    assert same.version == a.version  # thứ tự nhập không đổi phiên bản
    more = worker_config(_settings(proxy_mode="worker", proxies=[*lines, "10.0.0.3:3128"]))
    assert more.version != a.version
    # Backend chọn proxy (central) / không proxy: worker không nhận danh sách.
    for mode in ("central", "off"):
        config = worker_config(_settings(proxy_mode=mode, proxies=lines))
        assert (list(config.shared_proxies), config.shared_proxies_hash) == ([], "")


def test_worker_ttl_must_cover_three_heartbeats() -> None:
    with pytest.raises(ValueError, match="GATEWAY_WORKER_TTL_SECONDS"):
        _settings(gateway_worker_ttl_seconds=20, gateway_heartbeat_seconds=7)
    assert _settings(gateway_worker_ttl_seconds=21, gateway_heartbeat_seconds=7)


def test_queue_max_wait_must_exceed_grace() -> None:
    assert _settings().fetch_queue_max_wait_seconds == 300
    with pytest.raises(ValueError, match="FETCH_QUEUE_MAX_WAIT_SECONDS"):
        _settings(fetch_queue_grace_seconds=30, fetch_queue_max_wait_seconds=30)
    assert _settings(fetch_queue_grace_seconds=5, fetch_queue_max_wait_seconds=6)


def test_worker_config_version_is_stable() -> None:
    a = worker_config(_settings(gateway_http_slots=8))
    assert a.version > 0
    assert worker_config(_settings(gateway_http_slots=8)).version == a.version
    # Cài đặt không đẩy xuống worker không đổi version.
    assert worker_config(_settings(gateway_http_slots=8, poll_interval_seconds=99)).version == (
        a.version
    )
    assert worker_config(_settings(gateway_http_slots=9)).version != a.version
    assert config_version({"a": 1, "b": 2}) == config_version({"b": 2, "a": 1})
    assert config_version({"a": 1}) < 2**63


# --- token ---------------------------------------------------------------------------------------


def test_bearer_reads_authorization_metadata() -> None:
    assert bearer([("authorization", "Bearer s3cret")]) == "s3cret"
    assert bearer((("x", "y"), ("Authorization", b"Bearer s3cret"))) == "s3cret"
    assert bearer([("authorization", "s3cret")]) is None
    assert bearer([("authorization", "Bearer ")]) is None
    assert bearer([("x-token", "Bearer s3cret")]) is None
    assert bearer(None) is None


@pytest.mark.parametrize(
    ("peer", "ip"),
    [
        ("ipv4:10.0.0.5:51234", "10.0.0.5"),
        ("ipv6:[::1]:51234", "::1"),
        ("ipv6:%5B2001:db8::7%5D:443", "2001:db8::7"),
        ("unix:/tmp/gw.sock", None),
        ("ipv4:khong-phai-ip:1", None),
        (None, None),
    ],
)
def test_peer_ip(peer: str | None, ip: str | None) -> None:
    assert peer_ip(peer) == ip


def test_client_ip_trusts_forwarded_for_only_from_allowed_proxies() -> None:
    meta = [("authorization", "Bearer k"), ("x-forwarded-for", "198.51.100.9, 203.0.113.7")]
    nginx = "ipv4:172.18.0.4:40000"
    # Proxy tin cậy: địa chỉ cuối (proxy gần nhất ghi vào), giá trị client tự đặt phía trước bỏ.
    assert client_ip(nginx, meta, forwarded_allow_ips="172.18.0.0/16") == "203.0.113.7"
    assert client_ip(nginx, meta, forwarded_allow_ips="*") == "203.0.113.7"
    assert client_ip(nginx, [("X-Forwarded-For", b"203.0.113.8")], forwarded_allow_ips="*") == (
        "203.0.113.8"
    )
    # Không tin cậy / header hỏng / không có header: địa chỉ kết nối.
    assert client_ip(nginx, meta, forwarded_allow_ips="127.0.0.1,::1") == "172.18.0.4"
    assert client_ip(nginx, meta, forwarded_allow_ips="x, 10.0.0.0/8") == "172.18.0.4"
    assert client_ip(nginx, [("x-forwarded-for", "rác")], forwarded_allow_ips="*") == "172.18.0.4"
    assert client_ip(nginx, None, forwarded_allow_ips="*") == "172.18.0.4"
    assert client_ip("unix:/tmp/gw.sock", meta, forwarded_allow_ips="*") is None


def test_task_json_round_trip_keeps_unicode() -> None:
    # Body JSON tiếng Việt đi nguyên vẹn qua protobuf tới worker.
    task = crawl_pb2.FetchTask(body=encode_body({}, json={"q": "Hà Nội"})[1])
    again = crawl_pb2.FetchTask.FromString(task.SerializeToString())
    assert json.loads(again.body) == {"q": "Hà Nội"}
