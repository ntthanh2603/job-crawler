"""Cập nhật trực tiếp cho giao diện bằng Server-Sent Events: `GET /api/events`.

Một kết nối mỗi tab (trình duyệt chỉ mở 6 kết nối HTTP/1.1 tới một origin) mang nhiều chủ đề
(`topic`), mỗi sự kiện là một `LiveOut` cùng mô hình với endpoint JSON tương ứng:

- `workers`: như `GET /api/workers` (Redis của gateway).
- `queue`: như `GET /api/queue` với cùng tham số trang (Redis Streams + tên doanh nghiệp).
- `run`: như `GET /api/runs/{run_id}` (Postgres; log của lượt đang chạy ghi vào DB mỗi ~1 giây).

Mỗi process API có một `LiveHub`: mỗi chủ đề (kèm tham số) một task đọc lại mỗi `INTERVALS` giây
khi còn người nghe, mọi kết nối cùng chủ đề dùng chung lần đọc đó; chỉ đẩy khi nội dung đổi (so
băm JSON, bỏ trường chỉ là giờ máy chủ), không đổi quá `REFRESH_SECONDS` thì vẫn gửi lại để mốc
"x phút trước" trên giao diện không đứng yên. Nhiều replica API: mỗi replica tự đọc Redis / Postgres
dùng chung nên không cần pub/sub, cũng không cần sticky session.

Định dạng: sự kiện đầu chỉ có `retry` (ms trình duyệt chờ trước khi tự nối lại). Mỗi sự kiện dữ
liệu có `id` ghi dấu của mọi chủ đề đã gửi trên kết nối (`queue:1a2b…,workers:…`); trình duyệt tự
nối lại kèm `Last-Event-ID` thì chủ đề chưa đổi không gửi lại. Không có gì để gửi thì FastAPI gửi
comment `: ping` mỗi 15 giây để proxy / load balancer không cắt kết nối.

Đăng nhập như mọi endpoint (`GuardMiddleware`; EventSource gửi cookie `jc_token`). Luồng tự dừng khi
JWT hết hạn hoặc bị thu hồi (xét mỗi `AUTH_CHECK_SECONDS`): trình duyệt nối lại, nhận 401, giao
diện quay về poll rồi chuyển tới trang đăng nhập.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent

from jobcrawler.config import Settings
from jobcrawler.logging import get_logger
from jobcrawler.resources import Resources
from jobcrawler.web import auth as auth_tokens
from jobcrawler.web.api import (
    ApiOut,
    QueuePageOut,
    RunDetailOut,
    load_queue_page,
    load_run_detail,
)
from jobcrawler.web.settings_api import WorkersPageOut, load_workers_page

router = APIRouter(prefix="/api", tags=["live"])

log = get_logger(__name__)

LiveTopic = Literal["workers", "queue", "run"]
TOPICS: tuple[LiveTopic, ...] = ("workers", "queue", "run")

# Mỗi chủ đề đọc lại sau chừng này giây (khi còn người nghe).
INTERVALS: dict[LiveTopic, float] = {"workers": 2.0, "queue": 2.0, "run": 2.0}
# Nội dung không đổi quá chừng này giây thì vẫn gửi lại (mốc thời gian tương đối trên giao diện).
REFRESH_SECONDS = 30.0
# Người nghe cuối rời đi: giữ task đọc thêm chừng này giây (giao diện nối lại khi đổi trang).
LINGER_SECONDS = 10.0
# Trình duyệt chờ chừng này ms rồi tự nối lại khi mất kết nối.
RETRY_MS = 3000
# Xét lại hạn / thu hồi JWT của luồng sau chừng này giây.
AUTH_CHECK_SECONDS = 30.0
# Trường chỉ là giờ máy chủ lúc đọc: không tính vào dấu (không thì lần đọc nào cũng "đổi").
VOLATILE: dict[LiveTopic, Any] = {"workers": {"workers": {"now_ms"}}}
MAX_EVENT_ID = 512
HEX = "0123456789abcdef"


class LiveOut(ApiOut):
    """Một sự kiện của `GET /api/events`: trường tên `topic` có dữ liệu (cùng mô hình với
    `GET /api/workers`, `GET /api/queue`, `GET /api/runs/{run_id}`), hai trường còn lại null.
    `run` null khi không có lượt chạy này."""

    topic: LiveTopic
    workers: WorkersPageOut | None = None
    queue: QueuePageOut | None = None
    run: RunDetailOut | None = None


@dataclass(frozen=True, slots=True)
class TopicKey:
    """Chủ đề kèm tham số: queue — trang của ba hàng đợi; run — id lượt chạy."""

    topic: LiveTopic
    params: tuple[Any, ...] = ()


type Loader = Callable[[TopicKey], Awaitable[LiveOut]]


def resource_loader(res: Resources, base: Settings) -> Loader:
    """Đọc dữ liệu của một chủ đề như endpoint JSON tương ứng. `base`: Settings gốc của process."""

    async def load(key: TopicKey) -> LiveOut:
        if key.topic == "workers":
            return LiveOut(topic="workers", workers=await load_workers_page(res, base))
        if key.topic == "queue":
            page, poll_page, board_page = key.params
            queue = await load_queue_page(
                res, page=page, poll_page=poll_page, board_page=board_page
            )
            return LiveOut(topic="queue", queue=queue)
        (run_id,) = key.params
        return LiveOut(topic="run", run=await load_run_detail(res, run_id))

    return load


@dataclass(frozen=True, slots=True)
class Snapshot:
    topic: LiveTopic
    digest: str  # băm JSON (bỏ VOLATILE)
    data: str  # JSON của LiveOut: tuần tự hoá một lần cho mọi kết nối


def event_id(sent: Mapping[str, str]) -> str:
    """`id` của sự kiện: dấu của mọi chủ đề đã gửi trên kết nối."""
    return ",".join(f"{topic}:{digest}" for topic, digest in sorted(sent.items()))


def parse_event_id(value: str | None) -> dict[str, str]:
    """Ngược của `event_id` (header `Last-Event-ID`); phần không hợp lệ bị bỏ qua."""
    out: dict[str, str] = {}
    if not value or len(value) > MAX_EVENT_ID:
        return out
    for part in value.split(","):
        topic, _, digest = part.strip().partition(":")
        if topic in TOPICS and 0 < len(digest) <= 32 and not digest.strip(HEX):
            out[topic] = digest
    return out


class _Inbox:
    """Bản mới nhất của từng chủ đề chờ gửi trên một kết nối: kết nối chậm chỉ nhận bản mới nhất."""

    def __init__(self) -> None:
        self._pending: dict[LiveTopic, Snapshot] = {}
        self._ready = asyncio.Event()

    def put(self, snapshot: Snapshot) -> None:
        self._pending[snapshot.topic] = snapshot
        self._ready.set()

    async def take(self, timeout: float) -> list[Snapshot]:
        """Các bản đang chờ; chờ tối đa `timeout` giây, hết giờ thì danh sách rỗng."""
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(timeout):
                await self._ready.wait()
        self._ready.clear()
        items, self._pending = list(self._pending.values()), {}
        return items


class _Topic:
    """Một chủ đề trong process: task đọc định kỳ + các kết nối đang nghe."""

    def __init__(self, key: TopicKey, load: Loader, *, interval: float, refresh: float) -> None:
        self.key = key
        self._load = load
        self._interval = interval
        self._refresh = refresh
        self.snapshot: Snapshot | None = None
        self._published_at = 0.0
        self.inboxes: set[_Inbox] = set()
        self._task: asyncio.Task[None] | None = None
        self._failing = False

    def attach(self, inbox: _Inbox) -> None:
        self.inboxes.add(inbox)
        if self.snapshot is not None:
            inbox.put(self.snapshot)
        if self._task is None:
            # Context trống: task sống lâu hơn request mở nó, log không mang request id / người dùng
            # của request đó.
            self._task = asyncio.get_running_loop().create_task(
                self._run(), name=f"live:{self.key.topic}", context=contextvars.Context()
            )

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                out = await self._load(self.key)
            except Exception as exc:  # Redis / Postgres lỗi: giữ bản cũ, đọc lại lần sau
                if not self._failing:
                    log.warning("live.load_failed", topic=self.key.topic, error=repr(exc))
                self._failing = True
            else:
                if self._failing:
                    log.info("live.load_recovered", topic=self.key.topic)
                self._failing = False
                self._publish(out, loop.time())
            await asyncio.sleep(self._interval)

    def _publish(self, out: LiveOut, now: float) -> None:
        data = out.model_dump_json()
        volatile = VOLATILE.get(self.key.topic)
        basis = out.model_dump_json(exclude=volatile) if volatile else data
        digest = hashlib.blake2b(basis.encode(), digest_size=8).hexdigest()
        current = self.snapshot
        if (
            current is not None
            and current.digest == digest
            and now - self._published_at < self._refresh
        ):
            return
        self.snapshot = Snapshot(self.key.topic, digest, data)
        self._published_at = now
        for inbox in self.inboxes:
            inbox.put(self.snapshot)


class LiveHub:
    """Các chủ đề đang có người nghe trong process API (mỗi chủ đề một task đọc dùng chung)."""

    def __init__(
        self,
        load: Loader,
        *,
        intervals: Mapping[LiveTopic, float] = INTERVALS,
        refresh: float = REFRESH_SECONDS,
        linger: float = LINGER_SECONDS,
    ) -> None:
        self._load = load
        self._intervals = dict(intervals)
        self._refresh = refresh
        self._linger = linger
        self._topics: dict[TopicKey, _Topic] = {}

    @property
    def active(self) -> list[TopicKey]:
        """Chủ đề đang có task đọc (kể cả đang chờ dừng sau khi hết người nghe)."""
        return list(self._topics)

    @contextlib.contextmanager
    def _listen(self, keys: Iterable[TopicKey], inbox: _Inbox) -> Iterator[None]:
        topics: list[_Topic] = []
        for key in keys:
            topic = self._topics.get(key)
            if topic is None:
                topic = _Topic(
                    key, self._load, interval=self._intervals[key.topic], refresh=self._refresh
                )
                self._topics[key] = topic
            topic.attach(inbox)
            topics.append(topic)
        try:
            yield
        finally:
            loop = asyncio.get_running_loop()
            for topic in topics:
                topic.inboxes.discard(inbox)
                if not topic.inboxes:
                    loop.call_later(self._linger, self._reap, topic)

    def _reap(self, topic: _Topic) -> None:
        if topic.inboxes or self._topics.get(topic.key) is not topic:
            return  # có người nghe lại trong lúc chờ
        topic.stop()
        del self._topics[topic.key]

    def close(self) -> None:
        for topic in self._topics.values():
            topic.stop()
        self._topics.clear()

    async def stream(
        self,
        keys: list[TopicKey],
        *,
        known: Mapping[str, str] | None = None,
        alive: Callable[[], Awaitable[bool]] | None = None,
        check_every: float | None = None,
    ) -> AsyncIterator[ServerSentEvent]:
        """Sự kiện SSE của một kết nối. `known`: dấu từ `Last-Event-ID` — bản đầu tiên của chủ đề
        trùng dấu thì không gửi lại. `alive`: xét mỗi `check_every` giây (mặc định
        AUTH_CHECK_SECONDS), False thì kết thúc."""
        check_every = AUTH_CHECK_SECONDS if check_every is None else check_every
        # Chỉ bỏ qua bản đầu tiên trùng dấu: các lần gửi lại sau (REFRESH_SECONDS) vẫn đi.
        skip = {k.topic: known[k.topic] for k in keys if known and k.topic in known}
        sent = dict(skip)
        inbox = _Inbox()
        loop = asyncio.get_running_loop()
        with self._listen(keys, inbox):
            yield ServerSentEvent(retry=RETRY_MS)
            next_check = loop.time() + check_every
            while True:
                for snapshot in await inbox.take(max(0.0, next_check - loop.time())):
                    if skip.pop(snapshot.topic, None) == snapshot.digest:
                        continue
                    sent[snapshot.topic] = snapshot.digest
                    yield ServerSentEvent(raw_data=snapshot.data, id=event_id(sent))
                if loop.time() >= next_check:
                    if alive is not None and not await alive():
                        return
                    next_check = loop.time() + check_every


@dataclass(frozen=True, slots=True)
class LiveRequest:
    hub: LiveHub
    keys: list[TopicKey]
    known: dict[str, str]


def live_request(
    request: Request,
    topic: Annotated[
        list[LiveTopic],
        Query(description="Chủ đề cần theo dõi; nhiều chủ đề thì lặp lại tham số."),
    ],
    page: int = 1,
    poll_page: int = 1,
    board_page: int = 1,
    run_id: Annotated[uuid.UUID | None, Query(description="Lượt chạy của chủ đề `run`.")] = None,
    last_event_id: Annotated[
        str | None,
        Header(description="Trình duyệt tự gửi khi nối lại: `id` của sự kiện cuối đã nhận."),
    ] = None,
) -> LiveRequest:
    """Kiểm tham số trước khi mở luồng (lỗi thì trả JSON như endpoint thường)."""
    hub: LiveHub | None = getattr(request.app.state, "live", None)
    if hub is None:  # lifespan chưa mở tài nguyên
        raise HTTPException(503, "Chưa sẵn sàng")
    keys: list[TopicKey] = []
    for name in dict.fromkeys(topic):
        if name == "workers":
            keys.append(TopicKey("workers"))
        elif name == "queue":
            keys.append(TopicKey("queue", (max(1, page), max(1, poll_page), max(1, board_page))))
        elif run_id is None:
            raise HTTPException(422, "Chủ đề run cần tham số run_id")
        else:
            keys.append(TopicKey("run", (run_id,)))
    return LiveRequest(hub, keys, parse_event_id(last_event_id))


def _auth_check(request: Request) -> Callable[[], Awaitable[bool]] | None:
    """Đăng nhập bằng JWT: luồng còn hợp lệ khi token chưa hết hạn và chưa bị thu hồi."""
    claims: auth_tokens.Claims | None = getattr(request.state, "auth_claims", None)
    if claims is None:
        return None  # SSO qua proxy hoặc không đăng nhập
    res = getattr(request.app.state, "res", None)
    prefix = request.app.state.settings.redis_prefix

    async def alive() -> bool:
        if time.time() >= claims.expires_at:
            return False
        if res is None:
            return True
        try:
            return not await auth_tokens.is_revoked(res.redis, prefix, claims.jti)
        except Exception as exc:  # Redis lỗi: không cắt luồng của mọi người
            log.warning("auth.revoked_check_failed", error=str(exc))
            return True

    return alive


@router.get("/events", response_class=EventSourceResponse)
async def get_events(
    request: Request, live: Annotated[LiveRequest, Depends(live_request)]
) -> AsyncIterator[LiveOut]:
    """Luồng Server-Sent Events: mỗi sự kiện là một `LiveOut` (JSON trong `data`), gửi khi dữ
    liệu của chủ đề đổi. Dùng `EventSource` của trình duyệt (tự nối lại kèm `Last-Event-ID`)."""
    stream = live.hub.stream(live.keys, known=live.known, alive=_auth_check(request))
    async for event in stream:
        # FastAPI gửi nguyên ServerSentEvent (id, retry); kiểu khai báo là LiveOut để spec ghi mô
        # hình của `data`.
        yield event  # pyright: ignore[reportReturnType]
