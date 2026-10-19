"""Gateway gRPC + hàng việc trên Redis thật, với worker giả viết bằng stub gRPC (không cần mạng
ngoài). Worker giả trả lời theo hàm `handler`; worker thật (gói jobworker) có test riêng."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import grpc
import httpx
import pytest
import zstandard
from grpc_health.v1 import health_pb2, health_pb2_grpc
from redis.asyncio import Redis

from jcproto.v1 import crawl_pb2, crawl_pb2_grpc
from jobcrawler.config import Settings
from jobcrawler.gateway import registry
from jobcrawler.gateway.dispatch import (
    DispatchError,
    FetchClient,
    NoReplyError,
    NoWorkerError,
    push_results,
)
from jobcrawler.gateway.remote import RemoteRenderer, RemoteTransport
from jobcrawler.gateway.server import GatewayServer
from jobcrawler.net.http import HttpClient, HttpConfig
from jobcrawler.net.kv import MemoryKV, RedisKV
from jobcrawler.net.proxy_pool import ProxyPool, parse_proxy_line
from jobcrawler.net.ratelimit import MemoryRateLimiter
from jobcrawler.schemas import FetchOutcome
from jobcrawler.services import health

from .conftest import _env
from .servers import Site, closed_port, serve_proxy, serve_site

pytestmark = pytest.mark.integration

HTML = "<html><head><meta charset='utf-8'></head><body>Tuyển dụng kế toán</body></html>"
Handler = Callable[[crawl_pb2.FetchTask], crawl_pb2.FetchResult | Awaitable[crawl_pb2.FetchResult]]


def _redis_url() -> str:
    return _env("REDIS_URL") or Settings().redis_url


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(_redis_url())
    try:
        await client.ping()  # pyright: ignore[reportGeneralTypeIssues]
    except Exception:
        await client.aclose()
        pytest.skip("không kết nối được Redis")
    yield client
    await client.aclose()


@pytest.fixture
async def prefix(redis: Redis) -> AsyncIterator[str]:
    value = f"jc-test-{uuid4().hex[:10]}"
    yield value
    keys = [k async for k in redis.scan_iter(f"{value}:*")]
    if keys:
        await redis.delete(*keys)


def make_settings(prefix: str, **kw: Any) -> Settings:
    base: dict[str, Any] = {
        "redis_url": _redis_url(),
        "redis_prefix": prefix,
        "gateway_token": None,
        "fetch_queue_grace_seconds": 5,
        "gateway_worker_ttl_seconds": 10,
        "proxy_mode": "worker",
    }
    return Settings(**{**base, **kw})


@asynccontextmanager
async def gateway(redis: Redis, settings: Settings, **kw: Any) -> AsyncIterator[GatewayServer]:
    server = GatewayServer(redis, settings, config_cache_seconds=0, **kw)
    await server.start("127.0.0.1", 0)
    try:
        yield server
    finally:
        await server.stop(0)


@asynccontextmanager
async def fetch_client(redis: Redis, settings: Settings) -> AsyncIterator[FetchClient]:
    client = FetchClient(redis, settings)
    try:
        yield client
    finally:
        await client.close()


def http_client(client: FetchClient, *, proxies: ProxyPool | None = None) -> HttpClient:
    kv = MemoryKV()
    return HttpClient(
        RemoteTransport(client),
        kv=kv,
        proxies=proxies or ProxyPool(kv, []),
        ratelimiter=MemoryRateLimiter(),
        renderer=RemoteRenderer(client),
        config=HttpConfig(default_rate=1000, burst=100, timeout=5, backoff_base=0.01),
    )


def ok_result(task: crawl_pb2.FetchTask, body: bytes = HTML.encode(), **kw: Any) -> Any:
    fields: dict[str, Any] = {
        "task_id": task.task_id,
        "status": 200,
        "final_url": task.url,
        "headers": {"content-type": "text/html; charset=utf-8"},
        "body": body,
    }
    return crawl_pb2.FetchResult(**{**fields, **kw})


def default_handler(task: crawl_pb2.FetchTask) -> crawl_pb2.FetchResult:
    if task.url.endswith("/robots.txt"):
        return crawl_pb2.FetchResult(task_id=task.task_id, status=404, final_url=task.url)
    return ok_result(task)


def _channel(port: int) -> grpc.aio.Channel:
    return grpc.aio.insecure_channel(f"127.0.0.1:{port}")


class FakeWorker:
    """Worker tối giản: Register, vòng Lease → `handler` → Complete, Leave khi thoát."""

    def __init__(
        self,
        port: int,
        *,
        pool: str = "",
        http_slots: int = 4,
        render_slots: int = 1,
        handler: Handler = default_handler,
        token: str | None = None,
        complete_port: int | None = None,
        serve: bool = True,
    ) -> None:
        self.worker_id = f"fake-{uuid4().hex[:8]}"
        self.pool = pool
        self.http_slots = http_slots
        self.render_slots = render_slots
        self.handler = handler
        self.metadata = (("authorization", f"Bearer {token}"),) if token else None
        self._channel = _channel(port)
        self._complete_channel = _channel(complete_port) if complete_port else self._channel
        self.stub = crawl_pb2_grpc.CrawlGatewayStub(self._channel)
        self.complete_stub = crawl_pb2_grpc.CrawlGatewayStub(self._complete_channel)
        self.tasks: list[crawl_pb2.FetchTask] = []
        self.config: crawl_pb2.WorkerConfig | None = None
        self._serve = serve
        self._loop: asyncio.Task[None] | None = None

    async def register(self) -> crawl_pb2.WorkerConfig:
        info = crawl_pb2.WorkerInfo(
            worker_id=self.worker_id,
            hostname="test-host",
            version="0.0-test",
            pool=self.pool,
            http_slots=self.http_slots,
            render_slots=self.render_slots,
            local_proxies=1,
            labels={"region": "vn"},
            started_at_ms=int(time.time() * 1000),
        )
        resp = await self.stub.Register(
            crawl_pb2.RegisterRequest(info=info), metadata=self.metadata
        )
        self.config = resp.config
        return resp.config

    async def lease(self, wait_ms: int = 200, **kw: Any) -> crawl_pb2.LeaseResponse:
        request = crawl_pb2.LeaseRequest(
            worker_id=kw.pop("worker_id", self.worker_id),
            http_slots=kw.pop("http_slots", self.http_slots),
            render_slots=kw.pop("render_slots", self.render_slots),
            wait_ms=wait_ms,
        )
        return await self.stub.Lease(request, metadata=self.metadata)

    async def _handle(self, task: crawl_pb2.FetchTask) -> crawl_pb2.FetchResult:
        self.tasks.append(task)
        result = self.handler(task)
        if inspect.isawaitable(result):
            result = await result
        result.worker_id = self.worker_id
        return result

    async def _run(self) -> None:
        while True:
            resp = await self.lease()
            if not resp.tasks:
                continue
            results = await asyncio.gather(*(self._handle(t) for t in resp.tasks))
            await self.complete_stub.Complete(
                crawl_pb2.CompleteRequest(worker_id=self.worker_id, results=results),
                metadata=self.metadata,
            )

    async def __aenter__(self) -> FakeWorker:
        await self.register()
        if self._serve:
            self._loop = asyncio.get_running_loop().create_task(self._run())
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._loop is not None:
            self._loop.cancel()
            with contextlib.suppress(asyncio.CancelledError, grpc.aio.AioRpcError):
                await self._loop
        with contextlib.suppress(grpc.aio.AioRpcError):
            await self.stub.Leave(
                crawl_pb2.LeaveRequest(worker_id=self.worker_id), metadata=self.metadata
            )
        await self._channel.close()
        if self._complete_channel is not self._channel:
            await self._complete_channel.close()


def http_task(url: str, timeout: float = 5) -> crawl_pb2.FetchTask:
    return crawl_pb2.FetchTask(
        kind=crawl_pb2.TASK_KIND_HTTP, method="GET", url=url, timeout_seconds=timeout
    )


# --- luồng chính ---------------------------------------------------------------------------------


async def test_fetch_through_worker(redis: Redis, prefix: str) -> None:
    settings = make_settings(prefix)

    def handler(task: crawl_pb2.FetchTask) -> crawl_pb2.FetchResult:
        if task.url.endswith("/robots.txt"):
            return crawl_pb2.FetchResult(task_id=task.task_id, status=404, final_url=task.url)
        return ok_result(
            task,
            final_url=f"{task.url}?moved=1",
            headers={"Content-Type": "text/html; charset=utf-8", "X-Method": task.method},
            proxy_id="w:ab12",
        )

    async with (
        gateway(redis, settings) as gw,
        FakeWorker(gw.port, handler=handler) as worker,
        fetch_client(redis, settings) as client,
    ):
        http = http_client(client)
        page = await http.fetch("https://jobs.example.vn/careers", source="careers")
        posted = await http.fetch(
            "https://jobs.example.vn/api/search", method="POST", json={"q": "kế toán"}
        )
        status = await registry.load_worker(redis, registry.Keys(prefix), worker.worker_id)

    assert page.ok, page.error
    assert (page.text, page.final_url, page.proxy_id) == (
        HTML,
        "https://jobs.example.vn/careers?moved=1",
        "w:ab12",
    )
    assert page.headers["x-method"] == "GET"
    assert posted.ok
    assert posted.headers["x-method"] == "POST"
    robots, get, post = worker.tasks
    assert robots.url == "https://jobs.example.vn/robots.txt"
    assert get.headers["Accept-Language"].startswith("vi-VN")
    assert post.body == '{"q":"kế toán"}'.encode()
    assert post.headers["Content-Type"] == "application/json"
    assert all(t.task_id.startswith(f"{client.client_id}.") for t in worker.tasks)
    assert all(t.deadline_ms > time.time() * 1000 for t in worker.tasks)
    assert not any(t.direct or t.proxy for t in worker.tasks)
    assert client.stats() == {
        "submitted": 3,
        "completed": 3,
        "timeouts": 0,
        "no_worker": 0,
        "discarded": 0,
        "inflight": 0,
    }
    assert status is not None
    assert (status.leased, status.completed, status.labels) == (3, 3, {"region": "vn"})


async def test_render_with_captured_json_and_zstd(redis: Redis, prefix: str) -> None:
    settings = make_settings(prefix)
    page = "<div id='app'>Kiểm toán viên</div>".encode() * 50

    def handler(task: crawl_pb2.FetchTask) -> crawl_pb2.FetchResult:
        captured = crawl_pb2.CapturedJson(
            url="https://jobs.example.vn/api/jobs",
            method="POST",
            status=200,
            body=b'{"items": [1]}',
            request_json='{"page": 1}',
        )
        return ok_result(
            task,
            body=zstandard.ZstdCompressor().compress(page),
            body_zstd=True,
            captured=[captured],
        )

    async with (
        gateway(redis, settings) as gw,
        FakeWorker(gw.port, handler=handler) as worker,
        fetch_client(redis, settings) as client,
    ):
        result = await http_client(client).render(
            "https://jobs.example.vn/spa",
            check_robots=False,
            wait_until="networkidle",
            capture_json=True,
        )
    assert result.ok, result.error
    assert result.rendered
    assert result.content == page
    [captured] = result.captured
    assert (captured.method, captured.body, captured.request_json) == (
        "POST",
        b'{"items": [1]}',
        {"page": 1},
    )
    [task] = worker.tasks
    assert task.kind == crawl_pb2.TASK_KIND_RENDER
    assert (task.wait_until, task.capture_json) == ("networkidle", True)


async def test_no_worker_fails_fast(redis: Redis, prefix: str) -> None:
    settings = make_settings(prefix)
    async with gateway(redis, settings) as gw, fetch_client(redis, settings) as client:
        t0 = time.monotonic()
        with pytest.raises(NoWorkerError) as info:
            await client.submit(http_task("https://a.example.vn/"))
        assert time.monotonic() - t0 < 2
        assert info.value.kind == "other"
        result = await http_client(client).fetch(
            "https://a.example.vn/", retries=0, check_robots=False
        )
        assert (result.outcome, result.status) == (FetchOutcome.ERROR, None)
        assert result.error is not None
        assert "no_worker" in result.error

        # Worker không render được: việc render vẫn lỗi ngay, việc HTTP chạy.
        async with FakeWorker(gw.port, render_slots=0):
            await asyncio.sleep(0.3)  # quá LIVE_RETRY_SECONDS: client hỏi lại danh sách worker
            rendered = await http_client(client).render("https://a.example.vn/", check_robots=False)
            fetched = await http_client(client).fetch("https://a.example.vn/", check_robots=False)
    assert rendered.error is not None
    assert "no_worker" in rendered.error
    assert fetched.ok
    assert client.stats()["no_worker"] == 3


async def test_timeout_when_no_worker_replies(redis: Redis, prefix: str) -> None:
    settings = make_settings(prefix, fetch_queue_grace_seconds=0.5)
    async with (
        gateway(redis, settings) as gw,
        FakeWorker(gw.port, serve=False),  # đăng ký nhưng không xin việc
        fetch_client(redis, settings) as client,
    ):
        t0 = time.monotonic()
        with pytest.raises(NoReplyError) as info:
            await client.submit(http_task("https://slow.example.vn/", timeout=0.3))
        assert 0.7 <= time.monotonic() - t0 < 3
        assert info.value.kind == "timeout"
        result = await http_client(client).fetch(
            "https://slow.example.vn/", check_robots=False, retries=0, timeout=0.3
        )
        assert result.outcome is FetchOutcome.TIMEOUT
        # Kết quả tới muộn bị bỏ.
        await push_results(
            redis,
            registry.Keys(prefix),
            [crawl_pb2.FetchResult(task_id=f"{client.client_id}.1", status=200)],
        )
        for _ in range(50):
            if client.stats()["discarded"]:
                break
            await asyncio.sleep(0.05)
    stats = client.stats()
    assert (stats["timeouts"], stats["discarded"], stats["inflight"]) == (2, 1, 0)


async def test_expired_tasks_are_not_delivered(redis: Redis, prefix: str) -> None:
    settings = make_settings(prefix)
    keys = registry.Keys(prefix)
    stream = keys.stream("default", "http")
    async with gateway(redis, settings) as gw, FakeWorker(gw.port, serve=False) as worker:
        now = int(time.time() * 1000)
        for i, deadline in enumerate((now - 1000, now + 60_000, now - 5)):
            task = http_task(f"https://x.example.vn/{i}")
            task.task_id = f"c-1.{i}"
            task.deadline_ms = deadline
            await redis.xadd(stream, {"t": task.SerializeToString()})
        resp = await worker.lease(wait_ms=0)
        assert [t.url for t in resp.tasks] == ["https://x.example.vn/1"]
        assert await redis.xlen(stream) == 0  # đã giao (hoặc bỏ) thì xoá khỏi stream
        t0 = time.monotonic()
        assert not (await worker.lease(wait_ms=300)).tasks
        assert time.monotonic() - t0 >= 0.25
        # Không còn slot: không nhận việc nhưng vẫn chờ (worker không gọi dồn dập).
        task = http_task("https://x.example.vn/full")
        await redis.xadd(stream, {"t": task.SerializeToString()})
        t0 = time.monotonic()
        assert not (await worker.lease(wait_ms=300, http_slots=0, render_slots=0)).tasks
        assert time.monotonic() - t0 >= 0.25
        assert await redis.xlen(stream) == 1


async def test_lease_waits_then_returns_new_task(redis: Redis, prefix: str) -> None:
    settings = make_settings(prefix)
    async with (
        gateway(redis, settings) as gw,
        FakeWorker(gw.port, serve=False) as worker,
        fetch_client(redis, settings) as client,
    ):
        lease = asyncio.ensure_future(worker.lease(wait_ms=5000))
        await asyncio.sleep(0.2)
        submit = asyncio.ensure_future(client.submit(http_task("https://late.example.vn/")))
        t0 = time.monotonic()
        resp = await lease
        assert time.monotonic() - t0 < 2  # trả ngay khi có việc, không chờ hết wait_ms
        [task] = resp.tasks
        await worker.complete_stub.Complete(
            crawl_pb2.CompleteRequest(worker_id=worker.worker_id, results=[ok_result(task)])
        )
        result = await submit
    assert (result.status, result.body) == (200, HTML.encode())


async def test_complete_drops_malformed_task_ids(redis: Redis, prefix: str) -> None:
    settings = make_settings(prefix)
    async with gateway(redis, settings) as gw, FakeWorker(gw.port, serve=False) as worker:
        resp = await worker.complete_stub.Complete(
            crawl_pb2.CompleteRequest(
                worker_id=worker.worker_id,
                results=[
                    crawl_pb2.FetchResult(task_id="c-1.1", status=200),
                    crawl_pb2.FetchResult(task_id="bad", status=200),
                ],
            )
        )
        assert (resp.accepted, resp.dropped) == (1, 1)
        replies = registry.Keys(prefix).reply("c-1")
        assert await redis.llen(replies) == 1  # pyright: ignore[reportGeneralTypeIssues]


# --- vòng đời worker -----------------------------------------------------------------------------


async def test_unknown_worker_drain_heartbeat_leave(redis: Redis, prefix: str) -> None:
    settings = make_settings(prefix)
    keys = registry.Keys(prefix)
    async with gateway(redis, settings) as gw, FakeWorker(gw.port, serve=False) as worker:
        for call in (
            worker.lease(worker_id="ghost", wait_ms=0),
            worker.stub.Heartbeat(crawl_pb2.HeartbeatRequest(worker_id="ghost")),
        ):
            with pytest.raises(grpc.aio.AioRpcError) as info:
                await call
            assert info.value.code() == grpc.StatusCode.NOT_FOUND

        beat = await worker.stub.Heartbeat(
            crawl_pb2.HeartbeatRequest(
                worker_id=worker.worker_id,
                stats=crawl_pb2.WorkerStats(http_done=12, proxies_alive=3, cpu_percent=1.5),
            )
        )
        assert worker.config is not None
        assert (beat.config_version, beat.drain) == (worker.config.version, False)
        [status] = await registry.list_workers(redis, keys)
        assert (status.alive, status.stats.http_done, status.stats.proxies_alive) == (True, 12, 3)
        assert status.stats.cpu_percent == 1.5

        await redis.xadd(
            keys.stream("default", "http"), {"t": http_task("https://x.vn/").SerializeToString()}
        )
        await registry.request_drain(redis, keys, worker.worker_id)
        resp = await worker.lease(wait_ms=0)
        assert (resp.drain, len(resp.tasks)) == (True, 0)
        beat = await worker.stub.Heartbeat(crawl_pb2.HeartbeatRequest(worker_id=worker.worker_id))
        assert beat.drain
        assert await redis.xlen(keys.stream("default", "http")) == 1  # việc chờ worker khác

    # Thoát context: worker gọi Leave.
    assert await registry.live_pools(redis, keys) == {}
    [gone] = await registry.list_workers(redis, keys)
    assert (gone.alive, gone.worker_id) == (False, worker.worker_id)


async def test_register_rejects_bad_pool(redis: Redis, prefix: str) -> None:
    settings = make_settings(prefix)
    async with gateway(redis, settings) as gw:
        worker = FakeWorker(gw.port, pool="bad pool:*")
        with pytest.raises(grpc.aio.AioRpcError) as info:
            await worker.register()
        await worker.__aexit__()
    assert info.value.code() == grpc.StatusCode.INVALID_ARGUMENT


async def test_prune_removes_expired_worker(redis: Redis, prefix: str) -> None:
    settings = make_settings(prefix)
    keys = registry.Keys(prefix)
    async with gateway(redis, settings) as gw, FakeWorker(gw.port, serve=False) as worker:
        await worker.lease(wait_ms=0)  # tạo consumer
        await redis.zadd(keys.workers, {worker.worker_id: int(time.time() * 1000) - 1})
        assert await gw.servicer.prune() == [worker.worker_id]
        assert await registry.live_pools(redis, keys) == {}
        consumers = await redis.xinfo_consumers(keys.stream("default", "http"), "workers")
        assert consumers == []
        with pytest.raises(grpc.aio.AioRpcError) as info:
            await worker.lease(wait_ms=0)
        assert info.value.code() == grpc.StatusCode.NOT_FOUND
        await worker.register()  # đăng ký lại thì nhận việc tiếp
        assert not (await worker.lease(wait_ms=0)).drain


async def test_config_follows_settings_provider(redis: Redis, prefix: str) -> None:
    current = {"settings": make_settings(prefix, gateway_http_slots=7, proxy_mode="central")}
    async with (
        gateway(redis, current["settings"], settings_provider=lambda: current["settings"]) as gw,
        FakeWorker(gw.port, serve=False) as worker,
    ):
        assert worker.config is not None
        assert (worker.config.http_slots, worker.config.use_local_proxies) == (7, False)
        before = (await worker.lease(wait_ms=0)).config_version
        assert before == worker.config.version
        current["settings"] = make_settings(prefix, gateway_http_slots=9, proxy_mode="central")
        after = (await worker.lease(wait_ms=0)).config_version
        assert after != before
        assert (await worker.register()).http_slots == 9


# --- nhiều gateway, định tuyến, xác thực ---------------------------------------------------------


async def test_two_gateways_share_redis_and_route_by_domain(redis: Redis, prefix: str) -> None:
    settings = make_settings(prefix, fetch_routes={"a.example.vn": "pa", "b.example.vn": "pb"})
    async with (
        gateway(redis, settings) as gw1,
        gateway(redis, settings) as gw2,
        # Worker A xin việc ở gateway 1 nhưng trả kết quả ở gateway 2.
        FakeWorker(gw1.port, pool="pa", complete_port=gw2.port) as worker_a,
        FakeWorker(gw2.port, pool="pb") as worker_b,
        FakeWorker(gw2.port) as worker_default,
        fetch_client(redis, settings) as client,
    ):
        urls = [
            *(f"https://www.a.example.vn/{i}" for i in range(5)),
            *(f"https://b.example.vn/{i}" for i in range(5)),
            *(f"https://other.example.vn/{i}" for i in range(3)),
        ]
        results = await asyncio.gather(*(client.submit(http_task(u)) for u in urls))
    by_worker = {r.final_url: r.worker_id for r in results}
    assert all(r.status == 200 for r in results)
    assert {by_worker[u] for u in urls[:5]} == {worker_a.worker_id}
    assert {by_worker[u] for u in urls[5:10]} == {worker_b.worker_id}
    assert {by_worker[u] for u in urls[10:]} == {worker_default.worker_id}


async def test_route_without_live_pool_falls_back_to_default(redis: Redis, prefix: str) -> None:
    settings = make_settings(prefix, fetch_routes={"topcv.vn": "residential"})
    async with (
        gateway(redis, settings) as gw,
        FakeWorker(gw.port) as worker,
        fetch_client(redis, settings) as client,
    ):
        result = await client.submit(http_task("https://www.topcv.vn/viec-lam"))
    assert (result.status, result.worker_id) == (200, worker.worker_id)


async def test_token_auth(redis: Redis, prefix: str) -> None:
    settings = make_settings(prefix, gateway_token="s3cret")
    async with gateway(redis, settings) as gw:
        for token in (None, "wrong"):
            worker = FakeWorker(gw.port, token=token)
            with pytest.raises(grpc.aio.AioRpcError) as info:
                await worker.register()
            assert info.value.code() == grpc.StatusCode.UNAUTHENTICATED
            await worker.__aexit__()
        async with FakeWorker(gw.port, token="s3cret", serve=False) as worker:
            assert worker.config is not None
            assert not (await worker.lease(wait_ms=0)).drain
        async with _channel(gw.port) as channel:
            health_stub: Any = health_pb2_grpc.HealthStub(channel)
            check = await health_stub.Check(health_pb2.HealthCheckRequest(service=""))
    assert check.status == health_pb2.HealthCheckResponse.SERVING


async def test_fetch_client_close_fails_pending_tasks(redis: Redis, prefix: str) -> None:
    settings = make_settings(prefix)
    async with gateway(redis, settings) as gw, FakeWorker(gw.port, serve=False):
        client = FetchClient(redis, settings)
        pending = asyncio.ensure_future(client.submit(http_task("https://x.example.vn/", 30)))
        await asyncio.sleep(0.2)
        await client.close()
        with pytest.raises(DispatchError, match="đã đóng"):
            await pending
        with pytest.raises(DispatchError):
            await client.submit(http_task("https://x.example.vn/"))


async def test_run_gateway_entrypoint(redis: Redis, prefix: str) -> None:
    from jobcrawler.entrypoints.gateway import run_gateway

    port = closed_port()
    settings = make_settings(prefix, gateway_host="127.0.0.1", gateway_port=port)
    task = asyncio.ensure_future(run_gateway(settings))
    try:
        async with _channel(port) as channel:
            stub: Any = health_pb2_grpc.HealthStub(channel)
            for _ in range(100):
                try:
                    check = await stub.Check(health_pb2.HealthCheckRequest(), timeout=1)
                    break
                except grpc.aio.AioRpcError:
                    await asyncio.sleep(0.05)
            else:
                pytest.fail("gateway không lên")
        assert check.status == health_pb2.HealthCheckResponse.SERVING
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# --- kiểm tra proxy từ mạng của worker -----------------------------------------------------------


@pytest.fixture
def site() -> Iterator[Site]:
    routes: dict[str, Any] = {
        "/trace": (200, {"Content-Type": "text/plain"}, b"fl=1\nip=127.0.0.1\nloc=VN\n"),
    }
    with serve_site(routes) as s:
        yield s


async def real_fetch(task: crawl_pb2.FetchTask) -> crawl_pb2.FetchResult:
    """Worker giả tải thật bằng httpx qua `task.proxy` (như worker thật dùng curl_cffi)."""
    try:
        async with httpx.AsyncClient(proxy=task.proxy or None) as http:
            resp = await http.request(task.method, task.url, timeout=task.timeout_seconds)
    except httpx.ProxyError as exc:
        return crawl_pb2.FetchResult(
            task_id=task.task_id, error_kind=crawl_pb2.ERROR_KIND_PROXY, error=str(exc)
        )
    except httpx.HTTPError as exc:
        return crawl_pb2.FetchResult(
            task_id=task.task_id, error_kind=crawl_pb2.ERROR_KIND_CONNECT, error=repr(exc)
        )
    return crawl_pb2.FetchResult(
        task_id=task.task_id,
        status=resp.status_code,
        final_url=str(resp.url),
        headers=dict(resp.headers),
        body=resp.content,
        proxy_id=task.proxy_id,
    )


async def test_check_proxies_through_worker(redis: Redis, prefix: str, site: Site) -> None:
    settings = make_settings(prefix, proxy_check_url=f"{site.base}/trace")
    with serve_proxy() as live:
        dead = f"http://127.0.0.1:{closed_port()}"
        kv = RedisKV(redis, prefix=prefix)
        pool = ProxyPool(kv, [p for u in (live.url, dead) if (p := parse_proxy_line(u))])
        async with gateway(redis, settings) as gw, fetch_client(redis, settings) as client:
            res = SimpleNamespace(settings=settings, proxies=pool, http=http_client(client))
            # Không có worker: không đánh giá được proxy, không loại proxy nào.
            idle = await health.check_proxies(cast(Any, res), timeout=3)
            assert [r.ok for r in idle] == [False, False]
            assert not any(s.dead for s in await pool.states())

            async with FakeWorker(gw.port, handler=real_fetch):
                await asyncio.sleep(0.3)  # quá LIVE_RETRY_SECONDS: client thấy worker mới
                results = await health.check_proxies(cast(Any, res), timeout=3)
    by_url = {r.proxy: r for r in results}
    ok = by_url[live.url]
    assert (ok.ok, ok.exit_ip, ok.location) == (True, "127.0.0.1", "VN")
    assert not by_url[dead].ok
    assert f"{site.base}/trace" in live.requests
    states = {s.proxy.url: s for s in await pool.states()}
    assert (states[live.url].dead, states[dead].dead) == (False, True)
