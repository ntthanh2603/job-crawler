"""Gateway gRPC + hàng việc trên Redis thật, với worker giả viết bằng stub gRPC (không cần mạng
ngoài). Worker giả trả lời theo hàm `handler`; worker thật (gói jobworker) có test riêng."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
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
from jobcrawler.common.config import Settings
from jobcrawler.common.schemas import FetchOutcome
from jobcrawler.modules.audit import audit_service
from jobcrawler.modules.gateway import dispatch_service, registry_service
from jobcrawler.modules.gateway.dispatch_service import (
    DispatchError,
    FetchClient,
    NoReplyError,
    NoWorkerError,
    priority_fetches,
    push_results,
)
from jobcrawler.modules.gateway.remote_service import RemoteRenderer, RemoteTransport
from jobcrawler.modules.gateway.server_service import GatewayServer
from jobcrawler.services import health
from jobcrawler.services.net.http import HttpClient, HttpConfig
from jobcrawler.services.net.kv import MemoryKV, RedisKV
from jobcrawler.services.net.proxy_pool import ProxyPool, parse_proxy_line
from jobcrawler.services.net.ratelimit import MemoryRateLimiter

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
        "gateway_heartbeat_seconds": 3,
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
        self.worker_token: str | None = None
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
        if resp.worker_token:  # gateway bật xác thực: các lời gọi sau dùng token riêng
            self.worker_token = resp.worker_token
            self.metadata = (("authorization", f"Bearer {resp.worker_token}"),)
        return resp.config

    async def complete(self, *results: crawl_pb2.FetchResult) -> crawl_pb2.CompleteResponse:
        return await self.complete_stub.Complete(
            crawl_pb2.CompleteRequest(worker_id=self.worker_id, results=results),
            metadata=self.metadata,
        )

    async def leave(self) -> None:
        await self.stub.Leave(
            crawl_pb2.LeaveRequest(worker_id=self.worker_id), metadata=self.metadata
        )

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
        status = await registry_service.load_worker(
            redis, registry_service.Keys(prefix), worker.worker_id
        )

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
    assert not any(t.priority for t in worker.tasks)  # không phải lượt poll: stream thường
    assert client.stats() == {
        "submitted": 3,
        "completed": 3,
        "timeouts": 0,
        "no_worker": 0,
        "discarded": 0,
        "extended": 0,
        "inflight": 0,
    }
    assert status is not None
    assert (status.leased, status.completed, status.labels) == (3, 3, {"region": "vn"})
    assert (status.dropped, status.lost) == (0, 0)
    # Kết quả đã nhận thì worker không còn giữ việc.
    assert not await redis.exists(registry_service.Keys(prefix).inflight(worker.worker_id))


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
        await wait_len(redis, registry_service.Keys(prefix).stream("default", "http"), 0)
        result = await http_client(client).fetch(
            "https://slow.example.vn/", check_robots=False, retries=0, timeout=0.3
        )
        assert result.outcome is FetchOutcome.TIMEOUT
        # Kết quả tới muộn bị bỏ.
        await push_results(
            redis,
            registry_service.Keys(prefix),
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
    keys = registry_service.Keys(prefix)
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


async def queue_task(redis: Redis, keys: registry_service.Keys, task_id: str, **kw: Any) -> None:
    """Việc vào stream như `FetchClient.submit` ghi (`t` + `id` + `d`)."""
    task = http_task(f"https://x.example.vn/{task_id}")
    task.task_id = task_id
    task.deadline_ms = kw.pop("deadline_ms", int(time.time() * 1000) + 60_000)
    fields = {"t": task.SerializeToString(), "id": task_id, "d": task.deadline_ms}
    await redis.xadd(keys.stream("default", "http", **kw), fields)  # pyright: ignore[reportArgumentType]


async def replies(redis: Redis, keys: registry_service.Keys, client_id: str) -> list[Any]:
    raw = await redis.lrange(keys.reply(client_id), 0, -1)  # pyright: ignore[reportGeneralTypeIssues]
    return [crawl_pb2.FetchResult.FromString(b) for b in raw]


async def test_complete_accepts_only_tasks_the_worker_holds(redis: Redis, prefix: str) -> None:
    settings = make_settings(prefix)
    keys = registry_service.Keys(prefix)
    async with (
        gateway(redis, settings) as gw,
        FakeWorker(gw.port, serve=False) as worker,
        FakeWorker(gw.port, serve=False) as other,
    ):
        await queue_task(redis, keys, "c-1.1")
        [task] = (await worker.lease(wait_ms=0)).tasks
        held = await redis.hgetall(keys.inflight(worker.worker_id))  # pyright: ignore[reportGeneralTypeIssues]
        deadline, leased = held[b"c-1.1"].decode().split(":")
        assert list(held) == [b"c-1.1"]
        assert int(deadline) == task.deadline_ms
        assert abs(int(leased) - time.time() * 1000) < 5000
        ttl = await redis.pttl(keys.inflight(worker.worker_id))
        assert task.deadline_ms - time.time() * 1000 < ttl <= task.deadline_ms + 130_000

        # Worker khác trả kết quả cho việc nó không giữ: bỏ.
        resp = await other.complete(crawl_pb2.FetchResult(task_id="c-1.1", status=500))
        assert (resp.accepted, resp.dropped) == (0, 1)
        resp = await worker.complete(
            crawl_pb2.FetchResult(task_id="c-1.1", status=200),
            crawl_pb2.FetchResult(task_id="bad", status=200),
            crawl_pb2.FetchResult(task_id="c-1.2", status=200),  # chưa từng giao
        )
        assert (resp.accepted, resp.dropped) == (1, 2)
        # Trả lần hai (Complete thử lại): việc không còn giữ, bỏ.
        resp = await worker.complete(crawl_pb2.FetchResult(task_id="c-1.1", status=200))
        assert (resp.accepted, resp.dropped) == (0, 1)
        [result] = await replies(redis, keys, "c-1")
        assert result.status == 200
        assert not await redis.exists(keys.inflight(worker.worker_id))
        status = await registry_service.load_worker(redis, keys, worker.worker_id)
        assert status is not None
        assert (status.leased, status.completed, status.dropped) == (1, 1, 2)


# --- vòng đời worker -----------------------------------------------------------------------------


async def test_unknown_worker_drain_heartbeat_leave(redis: Redis, prefix: str) -> None:
    settings = make_settings(prefix)
    keys = registry_service.Keys(prefix)
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
        [status] = await registry_service.list_workers(redis, keys)
        assert (status.alive, status.stats.http_done, status.stats.proxies_alive) == (True, 12, 3)
        assert status.stats.cpu_percent == 1.5

        await redis.xadd(
            keys.stream("default", "http"), {"t": http_task("https://x.vn/").SerializeToString()}
        )
        await registry_service.request_drain(redis, keys, worker.worker_id)
        resp = await worker.lease(wait_ms=0)
        assert (resp.drain, len(resp.tasks)) == (True, 0)
        beat = await worker.stub.Heartbeat(crawl_pb2.HeartbeatRequest(worker_id=worker.worker_id))
        assert beat.drain
        assert await redis.xlen(keys.stream("default", "http")) == 1  # việc chờ worker khác

    # Thoát context: worker gọi Leave.
    assert await registry_service.live_pools(redis, keys) == {}
    [gone] = await registry_service.list_workers(redis, keys)
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
    keys = registry_service.Keys(prefix)
    async with gateway(redis, settings) as gw, FakeWorker(gw.port, serve=False) as worker:
        now = int(time.time() * 1000)
        await queue_task(redis, keys, "c-1.1", high=True)
        await queue_task(redis, keys, "c-1.2")
        # Việc đã quá hạn khi worker mất: backend thôi chờ rồi, không trả.
        await queue_task(redis, keys, "c-1.3", deadline_ms=now + 1500)
        assert len((await worker.lease(wait_ms=0)).tasks) == 3  # tạo consumer, giữ 3 việc
        await asyncio.sleep(1.6)
        await redis.zadd(keys.workers, {worker.worker_id: int(time.time() * 1000) - 1})
        assert await gw.servicer.prune() == [worker.worker_id]
        assert await registry_service.live_pools(redis, keys) == {}
        lost = await replies(redis, keys, "c-1")
        assert sorted(r.task_id for r in lost) == ["c-1.1", "c-1.2"]
        assert all(r.error_kind == crawl_pb2.ERROR_KIND_WORKER_LOST for r in lost)
        assert all(r.worker_id == worker.worker_id for r in lost)
        assert not await redis.exists(keys.inflight(worker.worker_id))
        # Worker thật ra còn sống, trả kết quả muộn: bỏ (backend đã thử lại).
        resp = await worker.complete(crawl_pb2.FetchResult(task_id="c-1.1", status=200))
        assert (resp.accepted, resp.dropped) == (0, 1)
        status = await registry_service.load_worker(redis, keys, worker.worker_id)
        assert status is not None
        assert (status.lost, status.dropped) == (2, 1)
        with pytest.raises(grpc.aio.AioRpcError) as info:
            await worker.lease(wait_ms=0)
        assert info.value.code() == grpc.StatusCode.NOT_FOUND
        await worker.register()  # đăng ký lại thì nhận việc tiếp
        assert not (await worker.lease(wait_ms=0)).drain


async def test_prune_skips_worker_that_registered_again(redis: Redis, prefix: str) -> None:
    # Prune đọc danh sách quá hạn, worker kịp Register lại và nhận việc mới trước khi prune xử lý
    # tới nó: việc mới không bị trả `worker_lost` oan.
    settings = make_settings(prefix)
    keys = registry_service.Keys(prefix)
    async with gateway(redis, settings) as gw, FakeWorker(gw.port, serve=False) as worker:
        await redis.zadd(keys.workers, {worker.worker_id: int(time.time() * 1000) - 1})
        assert await registry_service.expired_workers(redis, keys) == [worker.worker_id]
        await worker.register()
        await queue_task(redis, keys, "c-1.1")
        assert len((await worker.lease(wait_ms=0)).tasks) == 1
        assert await dispatch_service.release_expired_worker(redis, keys, worker.worker_id) is None
        assert await gw.servicer.prune() == []
        assert await redis.hexists(keys.inflight(worker.worker_id), "c-1.1")  # pyright: ignore[reportGeneralTypeIssues]
        assert await replies(redis, keys, "c-1") == []
        assert await registry_service.live_pools(redis, keys) == {"default": 1}


async def test_lease_drops_expired_and_records_in_one_step(redis: Redis, prefix: str) -> None:
    settings = make_settings(prefix)
    keys = registry_service.Keys(prefix)
    stream = keys.stream("default", "http")
    async with gateway(redis, settings) as gw, FakeWorker(gw.port, serve=False) as worker:
        now = int(time.time() * 1000)
        await queue_task(redis, keys, "c-1.1", deadline_ms=now - 10)
        await queue_task(redis, keys, "c-1.2")
        # Entry kiểu cũ (chỉ `t`) vẫn được giao và ghi là worker đang giữ.
        legacy = http_task("https://x.example.vn/legacy")
        legacy.task_id = "c-1.3"
        legacy.deadline_ms = now + 60_000
        await redis.xadd(stream, {"t": legacy.SerializeToString()})
        resp = await worker.lease(wait_ms=0)
        assert sorted(t.task_id for t in resp.tasks) == ["c-1.2", "c-1.3"]
        assert await redis.xlen(stream) == 0
        held = await redis.hgetall(keys.inflight(worker.worker_id))  # pyright: ignore[reportGeneralTypeIssues]
        assert sorted(held) == [b"c-1.2", b"c-1.3"]
        assert await redis.get(keys.progress("default", "http")) == b"3"


async def test_heartbeat_reconciles_tasks_the_worker_holds(
    redis: Redis, prefix: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(prefix)
    keys = registry_service.Keys(prefix)
    async with gateway(redis, settings) as gw, FakeWorker(gw.port, serve=False) as worker:
        for task_id in ("c-1.1", "c-1.2"):
            await queue_task(redis, keys, task_id)
        assert len((await worker.lease(wait_ms=0)).tasks) == 2

        def beat(*task_ids: str, reports: bool = True) -> Any:
            return worker.stub.Heartbeat(
                crawl_pb2.HeartbeatRequest(
                    worker_id=worker.worker_id, task_ids=task_ids, reports_tasks=reports
                )
            )

        # Vừa giao (chưa quá ORPHAN_GRACE_MS): worker chưa kể cũng chưa coi là mất.
        resp = await beat("c-1.1")
        assert list(resp.revoked_task_ids) == []
        assert await redis.hlen(keys.inflight(worker.worker_id)) == 2  # pyright: ignore[reportGeneralTypeIssues]

        monkeypatch.setattr(dispatch_service, "ORPHAN_GRACE_MS", 0)
        # Worker cũ (không kể việc): không đối soát.
        await beat(reports=False)
        assert await redis.hlen(keys.inflight(worker.worker_id)) == 2  # pyright: ignore[reportGeneralTypeIssues]
        # Worker không có c-1.2 (lời trả Lease không tới): trả `worker_lost` để backend gửi lại;
        # worker kể c-1.9 mà gateway không ghi nhận: thu hồi.
        resp = await beat("c-1.1", "c-1.9")
        assert list(resp.revoked_task_ids) == ["c-1.9"]
        [lost] = await replies(redis, keys, "c-1")
        assert (lost.task_id, lost.error_kind) == ("c-1.2", crawl_pb2.ERROR_KIND_WORKER_LOST)
        assert lost.error == dispatch_service.ORPHAN_ERROR
        held = await redis.hgetall(keys.inflight(worker.worker_id))  # pyright: ignore[reportGeneralTypeIssues]
        assert list(held) == [b"c-1.1"]
        status = await registry_service.load_worker(redis, keys, worker.worker_id)
        assert status is not None
        assert status.lost == 1
        # Kết quả muộn của việc đã trả `worker_lost`: bỏ; việc còn giữ: nhận.
        resp = await worker.complete(
            crawl_pb2.FetchResult(task_id="c-1.2", status=200),
            crawl_pb2.FetchResult(task_id="c-1.1", status=200),
        )
        assert (resp.accepted, resp.dropped) == (1, 1)


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
            # Vào bằng GATEWAY_TOKEN: nhận token riêng, các lời gọi sau dùng token đó.
            assert worker.worker_token is not None
            assert worker.worker_token.startswith("jcw_")
            assert not (await worker.lease(wait_ms=0)).drain
            # GATEWAY_TOKEN không dùng được cho Lease (chỉ để vào).
            worker.metadata = (("authorization", "Bearer s3cret"),)
            with pytest.raises(grpc.aio.AioRpcError) as info:
                await worker.lease(wait_ms=0)
            assert info.value.code() == grpc.StatusCode.UNAUTHENTICATED
        async with _channel(gw.port) as channel:
            health_stub: Any = health_pb2_grpc.HealthStub(channel)
            check = await health_stub.Check(health_pb2.HealthCheckRequest(service=""))
    assert check.status == health_pb2.HealthCheckResponse.SERVING


async def test_one_time_join_key_and_token_binding(redis: Redis, prefix: str) -> None:
    settings = make_settings(prefix, gateway_token="s3cret")
    async with gateway(redis, settings) as gw:
        auth = gw.servicer.auth
        assert auth is not None
        join = await auth.create_join_key(created_by="admin")
        assert await auth.join_key_status(join.id) == ("pending", None)
        async with FakeWorker(gw.port, token=join.key, serve=False) as worker:
            assert worker.worker_token is not None
            assert await auth.join_key_status(join.id) == ("used", worker.worker_id)
            # Huỷ khoá đã dùng (hộp thoại đóng ngay sau khi worker vào): không huỷ, giữ `used`.
            assert not await auth.revoke_join_key(join.id)
            assert await auth.join_key_status(join.id) == ("used", worker.worker_id)
            token = worker.worker_token
            # Khoá tham gia chỉ dùng một lần.
            again = FakeWorker(gw.port, token=join.key)
            with pytest.raises(grpc.aio.AioRpcError) as info:
                await again.register()
            assert info.value.code() == grpc.StatusCode.UNAUTHENTICATED
            await again.__aexit__()
            # Token gắn với worker này: worker khác dùng thì bị từ chối.
            other = FakeWorker(gw.port, token=token, serve=False)
            other.metadata = (("authorization", f"Bearer {token}"),)
            with pytest.raises(grpc.aio.AioRpcError) as info:
                await other.lease(wait_ms=0)
            assert info.value.code() == grpc.StatusCode.PERMISSION_DENIED
            await other.__aexit__()
        # Redis chỉ giữ băm của khoá / token.
        keys = [k.decode() async for k in redis.scan_iter(match=f"{prefix}:gw:*")]
        assert not any(join.key in k or token in k for k in keys)
        # Huỷ khoá chưa dùng.
        spare = await auth.create_join_key(created_by="admin")
        assert await auth.revoke_join_key(spare.id)
        assert await auth.join_key_status(spare.id) == ("expired", None)


async def test_live_worker_id_belongs_to_one_token(redis: Redis, prefix: str) -> None:
    """Worker khác (khoá tham gia, token riêng hay GATEWAY_TOKEN) không đăng ký được trùng id của
    worker đang sống (ALREADY_EXISTS: worker thử lại, giữ token), nên không Leave / Complete thay
    worker đó được. Worker khởi động lại với cùng id và token của nó vào ngay; id rời thì dùng lại
    được."""
    settings = make_settings(prefix, gateway_token="s3cret")
    keys = registry_service.Keys(prefix)
    async with gateway(redis, settings) as gw:
        auth = gw.servicer.auth
        assert auth is not None
        join = await auth.create_join_key(created_by="admin")
        intruder = FakeWorker(gw.port, token=join.key, serve=False)
        try:
            async with FakeWorker(gw.port, token="s3cret", serve=False) as victim:
                await queue_task(redis, keys, "c-1.1")
                assert len((await victim.lease(wait_ms=0)).tasks) == 1
                own_id = intruder.worker_id
                intruder.worker_id = victim.worker_id
                with pytest.raises(grpc.aio.AioRpcError) as info:
                    await intruder.register()
                assert info.value.code() == grpc.StatusCode.ALREADY_EXISTS
                # Khoá tham gia chưa bị dùng: vào được dưới id của chính nó.
                assert await auth.join_key_status(join.id) == ("pending", None)
                intruder.worker_id = own_id
                await intruder.register()
                assert await auth.join_key_status(join.id) == ("used", own_id)
                own = intruder.metadata
                for metadata in (own, (("authorization", "Bearer s3cret"),)):
                    intruder.metadata = metadata
                    intruder.worker_id = victim.worker_id
                    with pytest.raises(grpc.aio.AioRpcError) as info:
                        await intruder.register()
                    assert info.value.code() == grpc.StatusCode.ALREADY_EXISTS
                intruder.metadata = own
                with pytest.raises(grpc.aio.AioRpcError) as info:
                    await intruder.leave()  # token của nó vẫn gắn với id của nó
                assert info.value.code() == grpc.StatusCode.PERMISSION_DENIED
                assert await redis.hexists(keys.inflight(victim.worker_id), "c-1.1")  # pyright: ignore[reportGeneralTypeIssues]
                # Worker thật Register lại (đổi cấu hình / khởi động lại cùng id, token đã lưu).
                await victim.register()
                assert not (await victim.lease(wait_ms=0)).drain
            # Worker kia đã rời: id dùng lại được.
            await intruder.register()
            assert not (await intruder.lease(wait_ms=0)).drain
        finally:
            await intruder.__aexit__()


async def test_processes_sharing_a_token_file_can_rebind(redis: Redis, prefix: str) -> None:
    """Hai process cùng máy dùng chung WORKER_TOKEN_FILE (id khác nhau, cùng token): Register bằng
    token đó gắn lại token qua lại, không bị coi là trùng id của token khác."""
    settings = make_settings(prefix, gateway_token="s3cret")
    async with gateway(redis, settings) as gw:
        first = FakeWorker(gw.port, token="s3cret", serve=False)
        second = FakeWorker(gw.port, serve=False)
        try:
            await first.register()
            second.metadata = first.metadata
            await second.register()  # token gắn sang id thứ hai
            with pytest.raises(grpc.aio.AioRpcError) as info:
                await first.lease(wait_ms=0)
            assert info.value.code() == grpc.StatusCode.PERMISSION_DENIED
            for _ in range(2):  # worker gặp PERMISSION_DENIED thì Register lại bằng chính token
                await first.register()
                assert not (await first.lease(wait_ms=0)).drain
                await second.register()
                assert not (await second.lease(wait_ms=0)).drain
        finally:
            await first.__aexit__()
            await second.__aexit__()


async def test_join_key_use_queues_one_audit_event(redis: Redis, prefix: str) -> None:
    """Worker vào bằng khoá tham gia: một dòng `worker.joined` chờ trên Redis (scheduler ghi vào
    Postgres). Register lại bằng token riêng hay vào bằng GATEWAY_TOKEN không ghi."""
    settings = make_settings(prefix, gateway_token="s3cret")
    pending = audit_service.pending_key(prefix)
    async with gateway(redis, settings, forwarded_allow_ips="127.0.0.1") as gw:
        auth = gw.servicer.auth
        assert auth is not None
        join = await auth.create_join_key(created_by="admin")
        worker = FakeWorker(gw.port, pool="vn")
        # Qua nginx của `frontend` (127.0.0.1 nằm trong FORWARDED_ALLOW_IPS): IP thật của worker.
        worker.metadata = cast(
            Any, (("authorization", f"Bearer {join.key}"), ("x-forwarded-for", "203.0.113.7"))
        )
        try:
            await worker.register()
            assert await redis.llen(pending) == 1  # pyright: ignore[reportGeneralTypeIssues]
            await worker.register()  # khởi động lại: Register bằng token riêng
        finally:
            await worker.__aexit__()
        async with FakeWorker(gw.port, token="s3cret", serve=False):
            pass
    raw = await redis.lrange(pending, 0, -1)  # pyright: ignore[reportGeneralTypeIssues]
    assert len(raw) == 1
    item = json.loads(raw[0])
    assert {k: item[k] for k in ("action", "actor", "resource_type", "resource_id", "ip")} == {
        "action": audit_service.WORKER_JOINED,
        "actor": "worker",
        "resource_type": "worker",
        "resource_id": worker.worker_id,
        "ip": "203.0.113.7",
    }
    assert item["metadata"] == {
        "join_id": join.id,
        "hostname": "test-host",
        "pool": "vn",
        "version": "0.0-test",
    }
    assert join.key not in raw[0].decode()


# --- việc worker đang giữ, hàng ưu tiên, thời gian chờ -------------------------------------------


async def wait_len(redis: Redis, stream: str, n: int) -> None:
    for _ in range(200):
        if await redis.xlen(stream) == n:
            return
        await asyncio.sleep(0.02)
    pytest.fail(f"{stream}: {await redis.xlen(stream)} entry, chờ {n}")


async def test_worker_leaving_returns_held_tasks_for_retry(redis: Redis, prefix: str) -> None:
    settings = make_settings(prefix)
    async with (
        gateway(redis, settings) as gw,
        FakeWorker(gw.port, serve=False) as lost,
        fetch_client(redis, settings) as client,
    ):
        pending = asyncio.ensure_future(client.submit(http_task("https://a.example.vn/1", 30)))
        [task] = (await lost.lease(wait_ms=3000)).tasks
        t0 = time.monotonic()
        await lost.leave()  # rời khi chưa trả kết quả
        result = await asyncio.wait_for(pending, 5)
        assert time.monotonic() - t0 < 2  # không chờ hết 30 s + grace
        assert (result.task_id, result.error_kind, result.worker_id) == (
            task.task_id,
            crawl_pb2.ERROR_KIND_WORKER_LOST,
            lost.worker_id,
        )

        # Qua HttpClient (không retry): RemoteTransport gửi lại ngay, worker khác làm.
        await lost.register()
        fetch = asyncio.ensure_future(
            http_client(client).fetch("https://a.example.vn/2", check_robots=False, retries=0)
        )
        [task] = (await lost.lease(wait_ms=3000)).tasks
        async with FakeWorker(gw.port) as rescuer:
            await lost.leave()
            page = await asyncio.wait_for(fetch, 10)
        assert page.ok, page.error
        assert [t.url for t in rescuer.tasks] == ["https://a.example.vn/2"]
        resp = await lost.complete(ok_result(task))  # tới muộn: bỏ
        assert (resp.accepted, resp.dropped) == (0, 1)
    assert client.stats()["discarded"] == 0


async def test_priority_tasks_are_leased_first(redis: Redis, prefix: str) -> None:
    settings = make_settings(prefix)
    keys = registry_service.Keys(prefix)
    hi, normal = keys.streams("default", "http")
    async with (
        gateway(redis, settings) as gw,
        FakeWorker(gw.port, serve=False) as worker,
        fetch_client(redis, settings) as client,
    ):
        builds = [
            asyncio.ensure_future(client.submit(http_task(f"https://b.example.vn/{i}")))
            for i in range(3)
        ]
        await wait_len(redis, normal, 3)
        with priority_fetches():  # task con thừa hưởng mức ưu tiên
            polls = [
                asyncio.ensure_future(client.submit(http_task(f"https://p.example.vn/{i}")))
                for i in range(2)
            ]
        await wait_len(redis, hi, 2)
        first = (await worker.lease(wait_ms=0, http_slots=3, render_slots=0)).tasks
        assert sorted(t.url for t in first[:2]) == [
            "https://p.example.vn/0",
            "https://p.example.vn/1",
        ]
        assert first[2].url.startswith("https://b.example.vn/")
        assert [t.priority for t in first] == [1, 1, 0]
        rest = (await worker.lease(wait_ms=0)).tasks
        assert len(rest) == 2
        assert all(t.url.startswith("https://b.example.vn/") for t in rest)

        # Đang chờ (long-poll) thì việc ưu tiên mới tới cũng được giao ngay.
        lease = asyncio.ensure_future(worker.lease(wait_ms=3000))
        await asyncio.sleep(0.2)
        with priority_fetches():
            late = asyncio.ensure_future(client.submit(http_task("https://p.example.vn/late")))
        [urgent] = (await asyncio.wait_for(lease, 2)).tasks
        assert (urgent.url, urgent.priority) == ("https://p.example.vn/late", 1)

        await worker.complete(*(ok_result(t) for t in (*first, *rest, urgent)))
        results = await asyncio.gather(*builds, *polls, late)
    assert all(r.status == 200 for r in results)
    assert await redis.get(keys.progress("default", "http")) == b"6"


async def test_queue_wait_extends_while_queue_moves(redis: Redis, prefix: str) -> None:
    # Khung chờ 0.2 + 0.5 s; worker lấy một việc mỗi ~0.2 s nên việc cuối phải chờ ~1.5 s.
    settings = make_settings(prefix, fetch_queue_grace_seconds=0.5, fetch_queue_max_wait_seconds=5)
    async with (
        gateway(redis, settings) as gw,
        FakeWorker(gw.port, serve=False) as worker,
        fetch_client(redis, settings) as client,
    ):
        t0 = time.time() * 1000
        pending = [
            asyncio.ensure_future(client.submit(http_task(f"https://q.example.vn/{i}", 0.2)))
            for i in range(7)
        ]
        leased: list[crawl_pb2.FetchTask] = []
        while len(leased) < 7:
            resp = await worker.lease(wait_ms=500, http_slots=1, render_slots=0)
            leased.extend(resp.tasks)
            if resp.tasks:
                await worker.complete(*(ok_result(t) for t in resp.tasks))
            await asyncio.sleep(0.2)
        results = await asyncio.gather(*pending)
    assert all(r.status == 200 for r in results)
    stats = client.stats()
    assert stats["timeouts"] == 0
    assert stats["extended"] >= 1
    # Hạn của việc = lúc gửi + timeout + FETCH_QUEUE_MAX_WAIT_SECONDS.
    assert all(t0 + 5000 <= t.deadline_ms <= t0 + 6200 for t in leased)


async def test_queue_wait_stops_at_max_wait_and_unqueues(redis: Redis, prefix: str) -> None:
    settings = make_settings(
        prefix, fetch_queue_grace_seconds=0.3, fetch_queue_max_wait_seconds=1.0
    )
    stream = registry_service.Keys(prefix).stream("default", "http")
    async with (
        gateway(redis, settings) as gw,
        FakeWorker(gw.port, serve=False) as worker,
        fetch_client(redis, settings) as client,
    ):
        ahead = [
            asyncio.ensure_future(client.submit(http_task(f"https://h.example.vn/{i}", 5)))
            for i in range(12)
        ]
        await wait_len(redis, stream, 12)
        t0 = time.monotonic()
        late = asyncio.ensure_future(client.submit(http_task("https://h.example.vn/late", 0.2)))
        await wait_len(redis, stream, 13)
        leased: list[crawl_pb2.FetchTask] = []
        # Hàng việc chạy (một việc mỗi ~0.2 s) nhưng không tới lượt `late` trước 0.2 + 1.0 s.
        while not late.done():
            leased.extend((await worker.lease(wait_ms=0, http_slots=1, render_slots=0)).tasks)
            await asyncio.sleep(0.2)
        with pytest.raises(NoReplyError):
            await late
        assert 1.1 <= time.monotonic() - t0 < 2.5
        # Thôi chờ thì việc bị xoá khỏi stream: không worker nào tải thừa.
        await wait_len(redis, stream, 12 - len(leased))
        leased.extend((await worker.lease(wait_ms=0, http_slots=20, render_slots=0)).tasks)
        assert len(leased) == 12
        assert "https://h.example.vn/late" not in {t.url for t in leased}
        await worker.complete(*(ok_result(t) for t in leased))
        results = await asyncio.gather(*ahead)
    assert all(r.status == 200 for r in results)
    stats = client.stats()
    assert (stats["timeouts"], stats["completed"]) == (1, 12)
    assert stats["extended"] >= 1


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
    from jobcrawler.bootstrap.gateway import run_gateway

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
