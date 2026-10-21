"""Cập nhật trực tiếp (`GET /api/events`, `modules/live/live_service.py`) không cần DB: dữ liệu từ
loader
giả, đọc luồng SSE qua ASGI từng phần (httpx.ASGITransport chờ hết body nên không đọc được luồng vô
hạn)."""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from jobcrawler.bootstrap.app import create_app
from jobcrawler.common.config import Settings
from jobcrawler.modules.auth.auth_service import COOKIE, TokenAuth
from jobcrawler.modules.gateway.gateway_schema import WorkersPageOut, WorkersSummaryOut
from jobcrawler.modules.gateway.registry_service import WorkerStatus
from jobcrawler.modules.live import live_schema, live_service
from jobcrawler.modules.queue.queue_schema import QueueOut, QueuePageOut

PASSWORD = "x"
FAST: dict[live_schema.LiveTopic, float] = {"workers": 0.02, "queue": 0.02, "run": 0.02}


def workers_page(*ids: str, now_ms: int = 1) -> WorkersPageOut:
    summary = WorkersSummaryOut(
        alive=len(ids),
        gone=0,
        draining=0,
        pools={},
        http_slots=0,
        render_slots=0,
        http_inflight=0,
        render_inflight=0,
        done=0,
        failed=0,
    )
    workers = [WorkerStatus(worker_id=w) for w in ids]
    return WorkersPageOut(workers=workers, summary=summary, now_ms=now_ms, auth_enabled=False)


def queue_page(queued: int, page: int = 1) -> QueuePageOut:
    queue = QueueOut(
        name="build",
        queued=queued,
        active=0,
        scheduled=0,
        workers=1,
        capacity=4,
        complete=0,
        failed=0,
        retried=0,
        aborted=0,
        error=None,
        jobs=[],
        page=page,
        pages=1,
        total=queued,
    )
    return QueuePageOut(queues=[queue])


@dataclass
class FakeSource:
    """Dữ liệu hiện tại của từng chủ đề; đếm số lần đọc theo chủ đề."""

    workers: list[str] = field(default_factory=lambda: ["w-1"])
    queued: int = 0
    loads: Counter[str] = field(default_factory=Counter)
    pages: list[tuple[Any, ...]] = field(default_factory=list)
    fail: int = 0  # số lần đọc tới bị lỗi

    async def load(self, key: live_service.TopicKey) -> live_schema.LiveOut:
        self.loads[key.topic] += 1
        if self.fail:
            self.fail -= 1
            raise ConnectionError("redis down")
        if key.topic == "workers":
            # now_ms đổi mỗi lần đọc: không được tính là "đổi".
            page = workers_page(*self.workers, now_ms=int(time.time() * 1000))
            return live_schema.LiveOut(topic="workers", workers=page)
        if key.topic == "queue":
            self.pages.append(key.params)
            return live_schema.LiveOut(topic="queue", queue=queue_page(self.queued, key.params[0]))
        return live_schema.LiveOut(topic="run", run=None)


@dataclass
class Event:
    data: str | None = None
    id: str | None = None
    retry: int | None = None
    comments: list[str] = field(default_factory=list)

    @property
    def json(self) -> dict[str, Any]:
        assert self.data is not None
        return json.loads(self.data)


class SseStream:
    """Gọi app ASGI như trình duyệt mở EventSource: đọc body từng phần, đóng kết nối khi xong."""

    def __init__(self, app: Any, url: str, headers: dict[str, str] | None = None) -> None:
        self.app = app
        self.url = urlsplit(url)
        self.headers = {"host": "ui.test", "accept": "text/event-stream"} | {
            k.lower(): v for k, v in (headers or {}).items()
        }
        self.status = 0
        self.response_headers: dict[str, str] = {}
        self._chunks: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._started = asyncio.Event()
        self._closed = asyncio.Event()
        self._requested = False
        self._buffer = ""
        self._task: asyncio.Task[None] | None = None

    async def _receive(self) -> dict[str, Any]:
        if not self._requested:
            self._requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self._closed.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.response_headers = {k.decode(): v.decode() for k, v in message["headers"]}
            self._started.set()
        elif message["type"] == "http.response.body":
            if message.get("body"):
                await self._chunks.put(message["body"])
            if not message.get("more_body"):
                await self._chunks.put(None)

    async def __aenter__(self) -> SseStream:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},  # như uvicorn
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": self.url.path,
            "raw_path": self.url.path.encode(),
            "query_string": self.url.query.encode(),
            "root_path": "",
            "headers": [(k.encode(), v.encode()) for k, v in self.headers.items()],
            "client": ("127.0.0.1", 50000),
            "server": ("ui.test", 80),
        }
        self._task = asyncio.create_task(self.app(scope, self._receive, self._send))
        async with asyncio.timeout(5):
            await self._started.wait()
        return self

    async def __aexit__(self, *_: object) -> None:
        self._closed.set()
        assert self._task is not None
        async with asyncio.timeout(5):
            await self._task

    async def body(self) -> bytes:
        out = b""
        while (chunk := await self._chunks.get()) is not None:
            out += chunk
        return out

    async def next_event(self, timeout: float = 5) -> Event:
        """Khối kế tiếp (dữ liệu, retry hoặc chỉ comment); EOFError khi máy chủ đóng luồng."""
        async with asyncio.timeout(timeout):
            while "\n\n" not in self._buffer:
                chunk = await self._chunks.get()
                if chunk is None:
                    raise EOFError
                self._buffer += chunk.decode()
        block, self._buffer = self._buffer.split("\n\n", 1)
        event = Event()
        for line in block.split("\n"):
            name, _, value = line.partition(":")
            value = value.removeprefix(" ")
            if name == "":
                event.comments.append(value)
            elif name == "data":
                event.data = value if event.data is None else f"{event.data}\n{value}"
            elif name == "id":
                event.id = value
            elif name == "retry":
                event.retry = int(value)
        return event

    async def until_closed(self, timeout: float) -> None:
        """Đọc tới khi máy chủ đóng luồng (TimeoutError nếu quá `timeout` giây)."""
        async with asyncio.timeout(timeout):
            while await self._chunks.get() is not None:
                pass

    async def next_data(self, timeout: float = 5) -> Event:
        """Sự kiện có dữ liệu kế tiếp (bỏ qua retry / ping)."""
        async with asyncio.timeout(timeout):
            while True:
                event = await self.next_event(timeout)
                if event.data is not None:
                    return event


@dataclass
class Live:
    app: FastAPI
    source: FakeSource
    hub: live_service.LiveHub
    auth: TokenAuth

    def stream(self, query: str, **headers: str) -> SseStream:
        token = self.auth.issue().token
        return SseStream(
            self.app, f"/api/events?{query}", {"cookie": f"{COOKIE}={token}"} | headers
        )


@pytest.fixture
async def web() -> AsyncIterator[Live]:
    settings = Settings(_env_file=None, ui_password=SecretStr(PASSWORD))  # pyright: ignore[reportCallIssue]
    app = create_app(settings)
    # Không chạy lifespan (không DB / Redis): khoá ký JWT và hub gắn tay như lifespan.
    auth = TokenAuth(username="admin", password=PASSWORD, secret=b"k" * 32, hours=1)
    app.state.token_auth = auth
    source = FakeSource()
    hub = live_service.LiveHub(source.load, intervals=FAST, refresh=60, linger=0.05)
    app.state.live = hub
    yield Live(app, source, hub, auth)
    hub.close()


async def test_requires_login_and_reads_cookie(web: Live) -> None:
    async with SseStream(web.app, "/api/events?topic=workers") as anon:
        assert anon.status == 401
        assert json.loads(await anon.body()) == {"detail": "Cần đăng nhập"}
        assert anon.response_headers["www-authenticate"].startswith("Bearer")
    async with web.stream("topic=workers") as s:
        assert s.status == 200
        assert s.response_headers["content-type"].startswith("text/event-stream")
        assert s.response_headers["cache-control"] == "no-cache"
        assert s.response_headers["x-accel-buffering"] == "no"  # nginx không đệm
        first = await s.next_event()
        assert first.retry == live_service.RETRY_MS
        assert first.data is None


async def test_pushes_snapshot_then_only_changes(web: Live) -> None:
    async with web.stream("topic=workers") as s:
        event = await s.next_data()
        body = event.json
        assert body["topic"] == "workers"
        assert (body["queue"], body["run"]) == (None, None)
        assert [w["worker_id"] for w in body["workers"]["workers"]] == ["w-1"]
        assert (event.id or "").startswith("workers:")
        # Đọc lại nhiều lần, chỉ now_ms đổi: không gửi gì.
        loads = web.source.loads["workers"]
        with pytest.raises(TimeoutError):
            await s.next_data(timeout=0.3)
        assert web.source.loads["workers"] > loads + 3
        web.source.workers = ["w-1", "w-2"]
        changed = await s.next_data()
        assert [w["worker_id"] for w in changed.json["workers"]["workers"]] == ["w-1", "w-2"]
        assert changed.id != event.id


async def test_refresh_resends_unchanged_snapshot(web: Live) -> None:
    hub = live_service.LiveHub(web.source.load, intervals=FAST, refresh=0.2, linger=0.05)
    web.app.state.live = hub
    try:
        async with web.stream("topic=workers") as s:
            first = await s.next_data()
            again = await s.next_data(timeout=2)  # không đổi nhưng quá `refresh`: gửi lại
            assert again.id == first.id
            assert again.json["workers"]["now_ms"] > first.json["workers"]["now_ms"]
    finally:
        hub.close()


async def test_one_connection_many_topics_and_shared_reads(web: Live) -> None:
    query = "topic=workers&topic=queue&page=2&topic=queue"
    async with web.stream(query) as a, web.stream(query) as b:
        for s in (a, b):
            got = {(await s.next_data()).json["topic"] for _ in range(2)}
            assert got == {"workers", "queue"}
        # Hai kết nối, mỗi chủ đề (kèm tham số) một task đọc chung.
        assert sorted(k.topic for k in web.hub.active) == ["queue", "workers"]
        assert set(web.source.pages) == {(2, 1, 1)}
        web.source.queued = 3
        for s in (a, b):
            event = await s.next_data()
            assert event.json["queue"]["queues"][0]["queued"] == 3
            assert event.id is not None
            assert sorted(part.split(":")[0] for part in event.id.split(",")) == [
                "queue",
                "workers",
            ]
    # Hết người nghe: task đọc dừng sau `linger`.
    await asyncio.sleep(0.2)
    assert web.hub.active == []
    loads = sum(web.source.loads.values())
    await asyncio.sleep(0.1)
    assert sum(web.source.loads.values()) == loads


async def test_last_event_id_skips_unchanged_topics(web: Live) -> None:
    async with web.stream("topic=workers&topic=queue") as s:
        events = [await s.next_data() for _ in range(2)]
    last = events[-1].id
    assert last is not None
    async with web.stream("topic=workers&topic=queue", **{"Last-Event-ID": last}) as s:
        with pytest.raises(TimeoutError):  # client đã có bản này
            await s.next_data(timeout=0.3)
        web.source.queued = 5
        event = await s.next_data()
        assert event.json["topic"] == "queue"
        assert event.json["queue"]["queues"][0]["queued"] == 5
        assert event.id not in (None, last)
    # Id hỏng / lạ: bỏ qua, gửi lại đủ.
    async with web.stream("topic=workers", **{"Last-Event-ID": "workers:ZZ,khac:12"}) as s:
        assert (await s.next_data()).json["topic"] == "workers"


async def test_load_errors_keep_stream_open(web: Live) -> None:
    web.source.fail = 3
    async with web.stream("topic=workers") as s:
        event = await s.next_data()
        assert event.json["topic"] == "workers"
    assert web.source.loads["workers"] >= 4


async def test_run_topic_needs_run_id(web: Live) -> None:
    async with web.stream("topic=run") as s:
        assert s.status == 422
        assert json.loads(await s.body()) == {"detail": "Chủ đề run cần tham số run_id"}
    async with web.stream("topic=khac") as s:
        assert s.status == 422
    run_id = "0190f9a2-0000-7000-8000-000000000001"
    async with web.stream(f"topic=run&run_id={run_id}") as s:
        event = await s.next_data()
        assert event.json == {"topic": "run", "workers": None, "queue": None, "run": None}
        assert web.hub.active[0].params[0].hex == run_id.replace("-", "")


class RevokedRedis:
    def __init__(self) -> None:
        self.revoked: set[str] = set()

    async def exists(self, *names: str) -> int:
        return sum(1 for n in names if n in self.revoked)


async def test_stream_ends_when_token_is_revoked(
    web: Live, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(live_service, "AUTH_CHECK_SECONDS", 0.05)
    redis = RevokedRedis()
    web.app.state.res = SimpleNamespace(redis=redis)
    issued = web.auth.issue()
    headers = {"cookie": f"{COOKIE}={issued.token}"}
    async with SseStream(web.app, "/api/events?topic=workers", headers) as s:
        await s.next_data()
        redis.revoked.add(f"{web.app.state.settings.redis_prefix}:auth:revoked:{issued.jti}")
        await s.until_closed(timeout=2)


async def test_stream_ends_when_token_expires(web: Live, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live_service, "AUTH_CHECK_SECONDS", 0.05)
    # Token còn hạn đúng một giây.
    issued = web.auth.issue(now=time.time() - web.auth.ttl + 1)
    headers = {"cookie": f"{COOKIE}={issued.token}"}
    async with SseStream(web.app, "/api/events?topic=workers", headers) as s:
        assert s.status == 200
        await s.until_closed(timeout=3)


async def test_ping_keeps_idle_connection_alive(web: Live, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("fastapi.routing._PING_INTERVAL", 0.05)
    async with web.stream("topic=workers") as s:
        await s.next_data()
        ping = await s.next_event(timeout=2)
        assert (ping.data, ping.comments) == (None, ["ping"])


def test_event_id_roundtrip() -> None:
    sent = {"workers": "0a1b2c3d4e5f6071", "queue": "ffeeddccbbaa9988"}
    assert live_service.event_id(sent) == "queue:ffeeddccbbaa9988,workers:0a1b2c3d4e5f6071"
    assert live_service.parse_event_id(live_service.event_id(sent)) == sent
    assert live_service.parse_event_id(None) == {}
    assert live_service.parse_event_id("workers:XYZ,khac:12,run:,queue:ab") == {"queue": "ab"}
    assert live_service.parse_event_id("queue:ab" + "," * live_service.MAX_EVENT_ID) == {}
