"""Nhật ký thao tác của người vận hành (bảng `audit_events`, chỉ ghi thêm).

Hai đường ghi:

- `record(session, ...)`: ghi trong transaction của bên gọi, dùng khi dòng nhật ký phải đi cùng
  thay đổi trong DB (lưu cài đặt, kiểm tay): transaction rollback thì không có dòng nhật ký, ghi
  nhật ký lỗi thì thao tác cũng không được lưu.
- `record_safely(sessionmaker, ...)`: transaction riêng, không bao giờ ném lỗi (chỉ log cảnh báo),
  dùng sau thao tác không nằm trong một transaction DB (đưa job vào queue Redis, import CSV, yêu
  cầu worker dừng): ghi nhật ký lỗi không biến request đã thành công thành lỗi 500.

Người thao tác (`actor`): tên đăng nhập giao diện (`request.state.user` do `GuardMiddleware` gắn,
cũng là `app_settings.updated_by`) — `actor_of(user)` trả `anonymous` khi request không có người
dùng; `record` nhận `None` thì ghi `system` (thao tác không đến từ request: CLI, cron). `request_id`
đọc từ contextvars của structlog (middleware request id gắn vào); không có thì để trống.

Tên thao tác (`action`) có chấm và ổn định — trang Nhật ký lọc theo tên; thêm tên mới vào
`ACTIONS`. Không có hàm sửa / xoá dòng: bảng chỉ ghi thêm, bảo trì định kỳ không dọn bảng này.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobcrawler.db.models import AuditEvent
from jobcrawler.logging import get_logger

log = get_logger(__name__)

SYSTEM = "system"
ANONYMOUS = "anonymous"

# Mọi tên thao tác đang ghi (danh sách lọc của trang Nhật ký, `GET /api/audit/actions`).
SETTINGS_UPDATE = "settings.update"
SETTINGS_RESET = "settings.reset"
COMPANY_CREATE = "company.create"
COMPANY_BUILD = "company.build"
COMPANY_POLL = "company.poll"
COMPANIES_ENQUEUE = "companies.enqueue"
COMPANIES_IMPORT = "companies.import"
JOB_REVIEW = "job.review"
JOB_UNREVIEW = "job.unreview"
BOARD_INDEX = "board.index"
MAINTENANCE_RUN = "maintenance.run"
EVAL_RUN = "eval.run"
WORKER_DRAIN = "worker.drain"
WORKER_UNDRAIN = "worker.undrain"
WORKER_JOIN_KEY = "worker.join_key"
WORKER_JOIN_KEY_REVOKE = "worker.join_key_revoke"

ACTIONS: tuple[str, ...] = (
    SETTINGS_UPDATE,
    SETTINGS_RESET,
    COMPANY_CREATE,
    COMPANY_BUILD,
    COMPANY_POLL,
    COMPANIES_ENQUEUE,
    COMPANIES_IMPORT,
    JOB_REVIEW,
    JOB_UNREVIEW,
    BOARD_INDEX,
    MAINTENANCE_RUN,
    EVAL_RUN,
    WORKER_DRAIN,
    WORKER_UNDRAIN,
    WORKER_JOIN_KEY,
    WORKER_JOIN_KEY_REVOKE,
)

# Danh sách id trong `metadata` giữ tối đa chừng này phần tử (kèm tổng số), không ghi cả lô.
MAX_IDS = 20
MAX_LIMIT = 200


def actor_of(user: str | None) -> str:
    """Người thao tác của một request: tên đăng nhập, không có thì `anonymous`."""
    return user or ANONYMOUS


def current_request_id() -> str | None:
    value = structlog.contextvars.get_contextvars().get("request_id")
    return str(value) if value else None


def sample_ids(ids: Iterable[Any], limit: int = MAX_IDS) -> list[str]:
    """Tối đa `limit` id đầu (dạng chuỗi) để ghi vào `metadata` mà không phình dòng nhật ký."""
    out: list[str] = []
    for value in ids:
        if len(out) >= limit:
            break
        out.append(str(value))
    return out


def _event(
    *,
    action: str,
    actor: str | None,
    resource_type: str | None,
    resource_id: Any,
    changes: Mapping[str, Any] | None,
    metadata: Mapping[str, Any] | None,
) -> AuditEvent:
    # Bỏ các mục `None` của metadata cho dòng gọn; `changes` giữ nguyên (`None` là một giá trị).
    meta = {k: v for k, v in (metadata or {}).items() if v is not None}
    return AuditEvent(
        actor=actor or SYSTEM,
        action=action,
        resource_type=resource_type,
        resource_id=None if resource_id is None else str(resource_id),
        request_id=current_request_id(),
        changes=dict(changes) if changes else None,
        meta=meta or None,
    )


async def record(
    session: AsyncSession,
    *,
    action: str,
    actor: str | None,
    resource_type: str | None = None,
    resource_id: Any = None,
    changes: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> AuditEvent:
    """Ghi một dòng nhật ký trong transaction của bên gọi (cùng commit / rollback với thay đổi).
    Không bắt lỗi: ghi lỗi thì transaction của bên gọi hỏng theo. `changes` không được chứa giá
    trị bí mật (bên gọi tự che)."""
    event = _event(
        action=action,
        actor=actor,
        resource_type=resource_type,
        resource_id=resource_id,
        changes=changes,
        metadata=metadata,
    )
    session.add(event)
    await session.flush()
    return event


async def record_safely(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    action: str,
    actor: str | None,
    resource_type: str | None = None,
    resource_id: Any = None,
    changes: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> None:
    """Ghi một dòng nhật ký trong transaction riêng, sau khi thao tác đã xong. Không bao giờ ném
    lỗi (log cảnh báo `audit.write_failed`)."""
    try:
        async with sessionmaker.begin() as session:
            await record(
                session,
                action=action,
                actor=actor,
                resource_type=resource_type,
                resource_id=resource_id,
                changes=changes,
                metadata=metadata,
            )
    except Exception as exc:
        log.warning(
            "audit.write_failed",
            action=action,
            actor=actor,
            resource_type=resource_type,
            resource_id=None if resource_id is None else str(resource_id),
            error=f"{type(exc).__name__}: {exc}",
        )


# --- đọc -----------------------------------------------------------------------------------------


async def list_events(
    session: AsyncSession,
    *,
    limit: int = 50,
    before: UUID | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    actor: str | None = None,
) -> tuple[list[AuditEvent], UUID | None]:
    """Một trang nhật ký, mới nhất trước. Phân trang keyset theo `id` giảm dần (UUIDv7 tăng theo
    thời gian): trang sau = `before` là id dòng cuối trang trước. Trả (dòng, id cho trang sau
    hoặc None khi đã hết)."""
    limit = max(1, min(limit, MAX_LIMIT))
    stmt = select(AuditEvent)
    if before is not None:
        stmt = stmt.where(AuditEvent.id < before)
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    if resource_type:
        stmt = stmt.where(AuditEvent.resource_type == resource_type)
    if resource_id:
        stmt = stmt.where(AuditEvent.resource_id == resource_id)
    if actor:
        stmt = stmt.where(AuditEvent.actor == actor)
    rows = list((await session.scalars(stmt.order_by(AuditEvent.id.desc()).limit(limit + 1))).all())
    more = len(rows) > limit
    rows = rows[:limit]
    return rows, rows[-1].id if more and rows else None
