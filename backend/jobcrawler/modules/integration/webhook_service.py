"""Webhook ký HMAC tới hệ thống ngoài (docs/ARCHITECTURE.md mục 12, docs/INTEGRATION.md).

Relay (`relay_service`) chạy một vòng giao cho mỗi client bật webhook: đọc sự kiện sau
`api_clients.webhook_cursor` theo phạm vi của client (`feed_service.changes`), POST một lô JSON
(≤ `WEBHOOK_BATCH_SIZE` sự kiện, ≤ `MAX_BODY_BYTES`), bên nhận trả 2xx thì tiến cursor bằng
compare-and-set (hai relay chồng nhau trong lúc đổi leader không làm cursor lùi). Lỗi (mạng,
timeout, không phải 2xx, kể cả chuyển hướng) thì giữ cursor, lùi 1 s → 5 phút rồi gửi lại đúng lô
đó: giao ít nhất một lần, bên nhận khử trùng theo `seq`.

Ký: `X-JC-Signature: v1=<hex HMAC-SHA256(secret, "<X-JC-Timestamp>.<body>")>`, kèm
`X-JC-Timestamp` (giây Unix) và `X-JC-Delivery` (id lô, giữ nguyên khi gửi lại). Gọi thẳng từ
backend qua `res.api_http` như SERP / cảnh báo, không qua worker: đích là backend của đối tác,
không phải website cần thu thập.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import random
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobcrawler.common.logging import get_logger
from jobcrawler.database.ids import uuid7
from jobcrawler.database.models import ApiClient
from jobcrawler.modules.integration import feed_service
from jobcrawler.services import secretbox
from jobcrawler.services.resources import Resources

log = get_logger(__name__)

SECRET_PREFIX = "whsec_"
MAX_BODY_BYTES = 1024 * 1024
BACKOFF_MIN_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 300.0
# Tốc độ gửi tối đa tới một host bên nhận (request / giây, rate limit dùng chung của HttpClient).
WEBHOOK_RATE = 20.0
MAX_ERROR_CHARS = 500
USER_AGENT = "jobcrawler-webhook/1"

# Tên trường mã hoá (AAD của secretbox): đổi là không giải mã được dữ liệu cũ.
URL_FIELD = "api_clients.webhook_url"
SECRET_FIELD = "api_clients.webhook_secret"


def new_secret() -> str:
    return SECRET_PREFIX + secrets.token_urlsafe(32)


def seal(field_name: str, value: str | None) -> str | None:
    return None if value is None else secretbox.current().encrypt(field_name, value)


def unseal(field_name: str, value: str | None) -> str | None:
    if value is None:
        return None
    if not secretbox.is_encrypted(value):
        return value
    return secretbox.current().decrypt(field_name, value)


def signature(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return "v1=" + mac.hexdigest()


def headers(secret: str, body: bytes, *, delivery: str, now: float | None = None) -> dict[str, str]:
    timestamp = str(int(now if now is not None else time.time()))
    return {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "X-JC-Delivery": delivery,
        "X-JC-Timestamp": timestamp,
        "X-JC-Signature": signature(secret, timestamp, body),
    }


def backoff(failures: int) -> float:
    """Giây chờ trước lần gửi lại thứ `failures` (1 → 1 s, nhân đôi, tối đa 5 phút, ±20%)."""
    base = min(BACKOFF_MAX_SECONDS, BACKOFF_MIN_SECONDS * 2 ** max(0, failures - 1))
    return base * random.uniform(0.8, 1.2)


@dataclass(slots=True)
class Target:
    """Cấu hình webhook của một client lúc giao (đã giải mã)."""

    client_id: UUID
    name: str
    url: str
    secret: str
    scope: feed_service.Scope


@dataclass(slots=True)
class Attempt:
    """Kết quả một lần POST."""

    ok: bool
    status: int | None = None
    error: str | None = None
    ms: int = 0


@dataclass(slots=True)
class Delivery:
    """Kết quả một vòng giao: số sự kiện đã giao, còn sự kiện chờ không, lỗi."""

    sent: int = 0
    more: bool = False
    error: str | None = None
    attempts: list[Attempt] = field(default_factory=list)


async def targets(res: Resources) -> list[Target]:
    """Các client đang bật webhook (có URL + secret)."""
    async with res.sessionmaker() as session:
        rows = list(
            await session.scalars(
                select(ApiClient).where(
                    ApiClient.webhook_enabled.is_(True),
                    ApiClient.webhook_url.is_not(None),
                    ApiClient.webhook_secret.is_not(None),
                )
            )
        )
    out: list[Target] = []
    for c in rows:
        try:
            url, secret = unseal(URL_FIELD, c.webhook_url), unseal(SECRET_FIELD, c.webhook_secret)
        except Exception as exc:  # khoá mã hoá đã đổi mà chưa mã hoá lại: bỏ client này
            log.warning("webhook.unreadable", client=c.name, error=str(exc))
            continue
        if url and secret:
            scope = feed_service.Scope(c.id, c.all_companies, c.include_description)
            out.append(Target(c.id, c.name, url, secret, scope))
    return out


async def post(res: Resources, target: Target, body: bytes, *, delivery: str) -> Attempt:
    started = time.monotonic()
    result = await res.api_http.fetch(
        target.url,
        method="POST",
        data=body,
        headers=headers(target.secret, body, delivery=delivery),
        source="webhook",
        rate=WEBHOOK_RATE,
        use_cache=False,
        check_robots=False,
        timeout=res.settings.webhook_timeout_seconds,
        allow_redirects=False,
        retries=0,
    )
    ms = int((time.monotonic() - started) * 1000)
    status = result.status
    if status is not None and 200 <= status < 300:
        return Attempt(True, status, ms=ms)
    error = f"HTTP {status}" if status is not None else (result.error or result.outcome.value)
    return Attempt(False, status, error[:MAX_ERROR_CHARS], ms)


def _body(target: Target, page: Any, delivery: str) -> bytes:
    payload = {
        "delivery_id": delivery,
        "client": target.name,
        "events": [e.model_dump(mode="json") for e in page.events],
        "cursor": page.next_cursor,
        "head": page.head,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


async def _cursor(res: Resources, client_id: UUID) -> int | None:
    async with res.sessionmaker() as session:
        return await session.scalar(
            select(ApiClient.webhook_cursor).where(ApiClient.id == client_id)
        )


async def _advance(res: Resources, client_id: UUID, old: int, new: int, *, ok: bool) -> bool:
    """Tiến cursor từ `old` lên `new` (compare-and-set); `ok`: vừa giao thành công."""
    values: dict[str, Any] = {"webhook_cursor": new}
    if ok:
        values |= {
            "webhook_last_ok_at": datetime.now(UTC),
            "webhook_failing_since": None,
            "webhook_last_error": None,
        }
    async with res.sessionmaker.begin() as session:
        result = await session.execute(
            update(ApiClient)
            .where(ApiClient.id == client_id, ApiClient.webhook_cursor == old)
            .values(**values)
        )
    return bool(getattr(result, "rowcount", 0))


async def _failed(res: Resources, client_id: UUID, error: str) -> None:
    async with res.sessionmaker.begin() as session:
        client = await session.get(ApiClient, client_id, with_for_update=True)
        if client is None:
            return
        client.webhook_last_error = error[:MAX_ERROR_CHARS]
        if client.webhook_failing_since is None:
            client.webhook_failing_since = datetime.now(UTC)


async def deliver(res: Resources, target: Target, *, max_batches: int = 20) -> Delivery:
    """Giao tối đa `max_batches` lô cho một client. Dừng ở lỗi đầu tiên (cursor giữ nguyên)."""
    out = Delivery()
    batch = res.settings.webhook_batch_size
    for _ in range(max_batches):
        cursor = await _cursor(res, target.client_id)
        if cursor is None:  # client vừa bị xoá
            return out
        try:
            async with res.sessionmaker() as session:
                page = await feed_service.changes(
                    session, target.scope, cursor, limit=batch, now=datetime.now(UTC)
                )
                while len(page.events) > 1 and len(_body(target, page, "x")) > MAX_BODY_BYTES:
                    page = await feed_service.changes(
                        session,
                        target.scope,
                        cursor,
                        limit=max(1, len(page.events) // 2),
                        now=datetime.now(UTC),
                    )
        except feed_service.ResyncRequired:
            # Cursor đã bị dọn (webhook lỗi lâu hơn thời gian giữ) hoặc vượt head (DB khôi phục):
            # nhảy về mốc còn giữ và báo lỗi — bên nhận phải đồng bộ lại bằng snapshot.
            async with res.sessionmaker() as session:
                pruned, last = await feed_service.bounds(session)
            jump = pruned if cursor < pruned else last
            await _advance(res, target.client_id, cursor, jump, ok=False)
            await _failed(
                res,
                target.client_id,
                f"bỏ qua sự kiện ngoài khoảng còn giữ (cursor {cursor} → {jump}): cần đồng bộ lại",
            )
            continue
        new = int(page.next_cursor)
        if not page.events:
            if new > cursor:  # đoạn quét không có sự kiện của client: chỉ tiến cursor
                await _advance(res, target.client_id, cursor, new, ok=False)
                out.more = page.has_more
                if page.has_more:
                    continue
            return out
        delivery = str(uuid7())
        attempt = await post(res, target, _body(target, page, delivery), delivery=delivery)
        out.attempts.append(attempt)
        if not attempt.ok:
            out.error = attempt.error
            await _failed(res, target.client_id, attempt.error or "lỗi")
            log.warning(
                "webhook.failed",
                client=target.name,
                status=attempt.status,
                error=attempt.error,
                events=len(page.events),
            )
            return out
        await _advance(res, target.client_id, cursor, new, ok=True)
        out.sent += len(page.events)
        out.more = page.has_more
        log.info("webhook.delivered", client=target.name, events=len(page.events), ms=attempt.ms)
        if not page.has_more:
            return out
    return out


async def send_test(res: Resources, client: ApiClient) -> Attempt:
    """Gửi một lô thử (`events` rỗng, `test: true`) tới webhook của client, không đụng cursor."""
    url, secret = unseal(URL_FIELD, client.webhook_url), unseal(SECRET_FIELD, client.webhook_secret)
    if not url or not secret:
        return Attempt(False, error="chưa đặt URL hoặc secret webhook")
    target = Target(client.id, client.name, url, secret, feed_service.Scope(client.id))
    delivery = str(uuid7())
    body = json.dumps(
        {"delivery_id": delivery, "client": client.name, "test": True, "events": []},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return await post(res, target, body, delivery=delivery)


def url_hint(url: str | None) -> str | None:
    """URL webhook để hiện trên giao diện: bỏ query / fragment (có thể chứa token)."""
    if not url:
        return None
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}" + ("?…" if parts.query else "")


async def start_cursor(session: AsyncSession, client: ApiClient) -> None:
    """Bật webhook: lần đầu (hoặc cursor đã bị dọn) bắt đầu từ seq mới nhất — bên nhận lấy dữ
    liệu cũ bằng snapshot; tắt rồi bật lại thì tiếp tục từ chỗ dừng."""
    pruned, last = await feed_service.bounds(session)
    if client.webhook_cursor == 0 or client.webhook_cursor < pruned:
        client.webhook_cursor = last
    client.webhook_failing_since = None
    client.webhook_last_error = None


async def seal_stored(sessionmaker: async_sessionmaker[AsyncSession]) -> int:
    """Mã hoá (lại) URL / secret webhook còn lưu nguyên văn hoặc bằng khoá cũ (sau khi đổi
    ENCRYPTION_KEYS), như `app_settings_service.seal_stored`. Trả số client đã ghi lại."""
    box = secretbox.current()
    changed = 0
    async with sessionmaker.begin() as session:
        rows = await session.scalars(select(ApiClient).with_for_update())
        for client in rows:
            for attr, field_name in (("webhook_url", URL_FIELD), ("webhook_secret", SECRET_FIELD)):
                value = getattr(client, attr)
                if value is None or box.is_current(value):
                    continue
                try:
                    setattr(client, attr, seal(field_name, unseal(field_name, value)))
                    changed += 1
                except Exception as exc:  # khoá cũ đã bỏ khỏi ENCRYPTION_KEYS: không đọc được
                    log.warning("webhook.seal_failed", client=client.name, error=str(exc))
    if changed:
        log.info("webhook.sealed", fields=changed, key_id=box.active)
    return changed
