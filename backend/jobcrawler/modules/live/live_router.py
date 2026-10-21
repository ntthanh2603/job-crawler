"""Endpoint `GET /api/events` (Server-Sent Events): kiểm tham số, mở luồng của `LiveHub`
(`modules/live/live_service.py`), cắt luồng khi JWT hết hạn / bị thu hồi."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.sse import EventSourceResponse

from jobcrawler.common.logging import get_logger
from jobcrawler.modules.auth import auth_service
from jobcrawler.modules.live.live_schema import LiveOut, LiveTopic
from jobcrawler.modules.live.live_service import LiveHub, TopicKey, parse_event_id

log = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["live"])


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
    claims: auth_service.Claims | None = getattr(request.state, "auth_claims", None)
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
            return not await auth_service.is_revoked(res.redis, prefix, claims.jti)
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
