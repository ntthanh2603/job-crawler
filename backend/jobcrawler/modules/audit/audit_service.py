"""Nhật ký thao tác của người vận hành (bảng `audit_events`, chỉ ghi thêm).

Hai đường ghi:

- `record(session, ...)`: ghi trong transaction của bên gọi, dùng khi dòng nhật ký phải đi cùng
  thay đổi trong DB (lưu cài đặt, kiểm tay): transaction rollback thì không có dòng nhật ký, ghi
  nhật ký lỗi thì thao tác cũng không được lưu.
- `record_safely(sessionmaker, ...)`: transaction riêng, không bao giờ ném lỗi (chỉ log cảnh báo),
  dùng sau thao tác không nằm trong một transaction DB (đưa job vào queue Redis, import CSV, yêu
  cầu worker dừng): ghi nhật ký lỗi không biến request đã thành công thành lỗi 500.

Process không nối Postgres (gateway: worker dùng khoá tham gia, `worker.joined`) dùng đường thứ ba:
`queue_pending(redis, ...)` đẩy dòng vào list `<prefix>:audit:pending` trên Redis chính (id UUIDv7
và thời điểm lấy lúc xảy ra), scheduler chuyển vào bảng mỗi nhịp (`flush_pending`).

Người thao tác (`actor`): tên đăng nhập giao diện (`request.state.user` do `GuardMiddleware` gắn,
cũng là `app_settings.updated_by`) — `actor_of(user)` trả `anonymous` khi request không có người
dùng; `record` nhận `None` thì ghi `system` (thao tác không đến từ request: CLI, cron); worker vào
bằng khoá tham gia là `worker`. `request_id` đọc từ contextvars của structlog (middleware request
id gắn vào); `ip` / `user_agent` từ `REQUEST_INFO` (`GuardMiddleware` đặt qua `bind_request`);
không có thì để trống.

Đọc: `list_events` (keyset theo id) và `resource_labels` (tên hiện của đối tượng: tên doanh nghiệp,
tiêu đề việc làm — tra theo lô lúc đọc, không lưu vào dòng nhật ký). Lọc theo một khoá tham gia
(`resource_type=join_key` + `resource_id`) kèm dòng `worker.joined` của worker đã dùng khoá đó.

Tên thao tác (`action`) có chấm và ổn định — trang Nhật ký lọc theo tên; thêm tên mới vào
`ACTIONS`. Không có hàm sửa / xoá dòng: bảng chỉ ghi thêm, bảo trì định kỳ không dọn bảng này.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobcrawler.common.logging import get_logger
from jobcrawler.database.ids import uuid7
from jobcrawler.database.models import ApiClient, AuditEvent, Company, Job

log = get_logger(__name__)

SYSTEM = "system"
ANONYMOUS = "anonymous"
WORKER = "worker"  # worker lấy trang tự vào gateway bằng khoá tham gia

# Mọi tên thao tác đang ghi (danh sách lọc của trang Nhật ký, `GET /api/audit/actions`).
AUTH_LOGIN = "auth.login"
AUTH_LOGIN_FAILED = "auth.login_failed"
AUTH_LOGOUT = "auth.logout"
SETTINGS_UPDATE = "settings.update"
SETTINGS_RESET = "settings.reset"
SETTINGS_IMPORT_ENV = "settings.import_env"
PROXIES_CHECK = "proxies.check"
JOBS_EXPORT = "jobs.export"
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
WORKER_JOINED = "worker.joined"
API_CLIENT_CREATE = "api_client.create"
API_CLIENT_UPDATE = "api_client.update"
API_CLIENT_DELETE = "api_client.delete"
API_KEY_CREATE = "api_key.create"
API_KEY_REVOKE = "api_key.revoke"
API_WEBHOOK_SECRET = "api_client.webhook_secret"
API_WEBHOOK_TEST = "api_client.webhook_test"
# Hệ thống ngoài gọi API tích hợp (người thao tác `api:<tên client>`).
INTEGRATION_COMPANIES = "integration.companies"
INTEGRATION_REFRESH = "integration.refresh"

ACTIONS: tuple[str, ...] = (
    AUTH_LOGIN,
    AUTH_LOGIN_FAILED,
    AUTH_LOGOUT,
    SETTINGS_UPDATE,
    SETTINGS_RESET,
    SETTINGS_IMPORT_ENV,
    PROXIES_CHECK,
    JOBS_EXPORT,
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
    WORKER_JOINED,
    API_CLIENT_CREATE,
    API_CLIENT_UPDATE,
    API_CLIENT_DELETE,
    API_KEY_CREATE,
    API_KEY_REVOKE,
    API_WEBHOOK_SECRET,
    API_WEBHOOK_TEST,
    INTEGRATION_COMPANIES,
    INTEGRATION_REFRESH,
)

# Danh sách id trong `metadata` giữ tối đa chừng này phần tử (kèm tổng số), không ghi cả lô.
MAX_IDS = 20
MAX_LIMIT = 200
# User-Agent / tên đăng nhập sai giữ tối đa chừng này ký tự (giá trị do client gửi).
MAX_USER_AGENT = 300
MAX_ACTOR = 64


@dataclass(frozen=True, slots=True)
class RequestInfo:
    """Nguồn của request đang xử lý: IP người dùng và User-Agent."""

    ip: str | None
    user_agent: str | None


# Đặt ở `GuardMiddleware` cho mỗi request (task riêng của request nên không lẫn giữa request).
REQUEST_INFO: ContextVar[RequestInfo | None] = ContextVar("audit_request_info", default=None)


def bind_request(ip: str | None, user_agent: str | None) -> None:
    """Gắn nguồn request cho các dòng nhật ký ghi trong lúc xử lý request này."""
    ua = (user_agent or "").strip()[:MAX_USER_AGENT] or None
    REQUEST_INFO.set(RequestInfo(ip=ip or None, user_agent=ua))


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
    info = REQUEST_INFO.get()
    return AuditEvent(
        actor=(actor or SYSTEM)[:MAX_ACTOR],
        action=action,
        resource_type=resource_type,
        resource_id=None if resource_id is None else str(resource_id),
        request_id=current_request_id(),
        ip=info.ip if info else None,
        user_agent=info.user_agent if info else None,
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


# --- hàng chờ trên Redis (process không nối Postgres) --------------------------------------------

# Hàng chờ giữ tối đa chừng này dòng (scheduler không chạy lâu ngày thì bỏ dòng cũ nhất).
MAX_PENDING = 10_000
FLUSH_BATCH = 100


def pending_key(prefix: str) -> str:
    """List trên Redis chính (REDIS_URL, không phải cache): dòng nhật ký chờ ghi vào bảng."""
    return f"{prefix}:audit:pending"


async def queue_pending(
    redis: Any,
    prefix: str,
    *,
    action: str,
    actor: str,
    resource_type: str | None = None,
    resource_id: Any = None,
    metadata: Mapping[str, Any] | None = None,
    ip: str | None = None,
) -> bool:
    """Đẩy một dòng nhật ký vào hàng chờ trên Redis (process không nối Postgres: gateway);
    scheduler ghi vào bảng (`flush_pending`). Id (UUIDv7) và thời điểm lấy lúc gọi nên dòng nằm
    đúng chỗ trong danh sách dù được ghi muộn. Không bao giờ ném lỗi: trả False và log cảnh báo
    `audit.queue_failed`."""
    meta = {k: v for k, v in (metadata or {}).items() if v is not None}
    item = {
        "id": str(uuid7()),
        "occurred_at": datetime.now(UTC).isoformat(),
        "actor": actor[:MAX_ACTOR],
        "action": action,
        "resource_type": resource_type,
        "resource_id": None if resource_id is None else str(resource_id),
        "ip": ip or None,
        "metadata": meta or None,
    }
    key = pending_key(prefix)
    try:
        pipe = redis.pipeline(transaction=False)
        pipe.rpush(key, json.dumps(item, ensure_ascii=False))
        pipe.ltrim(key, -MAX_PENDING, -1)
        await pipe.execute()
    except Exception as exc:
        log.warning(
            "audit.queue_failed",
            action=action,
            resource_id=item["resource_id"],
            error=f"{type(exc).__name__}: {exc}",
        )
        return False
    return True


def _no_nul(value: Any) -> Any:
    """Bỏ ký tự NUL (Postgres không nhận trong text / JSONB): giá trị do worker tự gửi (tên máy,
    phiên bản) không được làm hỏng mãi một lô của hàng chờ."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {_no_nul(k): _no_nul(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_no_nul(v) for v in value]
    return value


def _pending_row(raw: bytes | str) -> dict[str, Any] | None:
    """Một mục của hàng chờ → giá trị cột của `AuditEvent`; mục hỏng thì None (bỏ, log cảnh báo)."""
    try:
        data = _no_nul(json.loads(raw))
        return {
            "id": UUID(data["id"]),
            "occurred_at": datetime.fromisoformat(data["occurred_at"]),
            "actor": str(data["actor"])[:MAX_ACTOR],
            "action": str(data["action"]),
            "resource_type": data.get("resource_type"),
            "resource_id": data.get("resource_id"),
            "request_id": None,
            "ip": data.get("ip"),
            "user_agent": None,
            "changes": None,
            "meta": data.get("metadata") or None,
        }
    except (ValueError, KeyError, TypeError) as exc:
        log.warning("audit.pending_dropped", error=f"{type(exc).__name__}: {exc}")
        return None


async def flush_pending(
    redis: Any,
    prefix: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    batch: int = FLUSH_BATCH,
) -> int:
    """Ghi các dòng trong hàng chờ (`queue_pending`) vào `audit_events` theo lô tới khi hết; trả số
    dòng mới ghi. Lấy lô bằng LPOP nhiều phần tử (nguyên tử: nhiều process gọi cùng lúc không lấy
    trùng). Ghi lỗi thì trả lô về đầu hàng (giữ thứ tự) rồi ném lỗi; dòng đã có (trùng id: lần ghi
    trước đã commit rồi mới báo lỗi) bỏ qua."""
    key = pending_key(prefix)
    total = 0
    while True:
        raw: list[Any] | None = await redis.lpop(key, batch)
        if not raw:
            return total
        rows = [row for item in raw if (row := _pending_row(item)) is not None]
        try:
            if rows:
                async with sessionmaker.begin() as session:
                    result = await session.execute(
                        pg_insert(AuditEvent)
                        .values(rows)
                        .on_conflict_do_nothing(index_elements=["id"])
                        .returning(AuditEvent.id)
                    )
                    total += len(result.all())
        except BaseException:
            await redis.lpush(key, *reversed(raw))
            raise
        if len(raw) < batch:
            return total


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
    hoặc None khi đã hết). Một khoá tham gia (`resource_type="join_key"` + `resource_id`): kèm dòng
    `worker.joined` của worker đã dùng khoá (id khoá nằm trong `metadata.join_id`)."""
    limit = max(1, min(limit, MAX_LIMIT))
    stmt = select(AuditEvent)
    if before is not None:
        stmt = stmt.where(AuditEvent.id < before)
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    if resource_type == "join_key" and resource_id:
        stmt = stmt.where(
            or_(
                and_(AuditEvent.resource_type == "join_key", AuditEvent.resource_id == resource_id),
                and_(
                    AuditEvent.action == WORKER_JOINED,
                    AuditEvent.meta["join_id"].as_string() == resource_id,
                ),
            )
        )
    else:
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


async def resource_labels(session: AsyncSession, events: Iterable[AuditEvent]) -> dict[str, str]:
    """Tên hiện của đối tượng theo `resource_id` (doanh nghiệp: tên thương hiệu / pháp lý / tên
    nhập; việc làm: "tiêu đề · doanh nghiệp"; hệ thống ngoài: tên). Đối tượng đã xoá hoặc loại
    khác thì không có."""
    ids: dict[str, set[UUID]] = {"company": set(), "job": set(), "api_client": set()}
    for e in events:
        if e.resource_type in ids and e.resource_id:
            try:
                ids[e.resource_type].add(UUID(e.resource_id))
            except ValueError:
                continue
    out: dict[str, str] = {}
    if ids["company"]:
        rows = await session.execute(
            select(
                Company.id,
                func.coalesce(Company.brand_name, Company.legal_name, Company.input_name),
            ).where(Company.id.in_(ids["company"]))
        )
        out |= {str(cid): name for cid, name in rows}
    if ids["job"]:
        rows = await session.execute(
            select(Job.id, Job.title, Job.company_name).where(Job.id.in_(ids["job"]))
        )
        out |= {
            str(jid): f"{title} · {company}" if company else title for jid, title, company in rows
        }
    if ids["api_client"]:
        rows = await session.execute(
            select(ApiClient.id, ApiClient.name).where(ApiClient.id.in_(ids["api_client"]))
        )
        out |= {str(cid): name for cid, name in rows}
    return out
