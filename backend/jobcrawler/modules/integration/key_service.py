"""Khoá API của hệ thống ngoài (`api_clients` / `api_keys`, docs/ARCHITECTURE.md mục 12).

Khoá dạng `jca_<43 ký tự urlsafe>`, chỉ hiện một lần lúc tạo; bảng chỉ giữ sha256 (khoá ngẫu
nhiên 256 bit: không cần hàm băm chậm) và vài ký tự đầu để nhận ra trên giao diện. Bền trong
Postgres (không mất khi Redis mất), khác khoá tham gia của worker.

`ApiKeyAuth` (GuardMiddleware dùng cho `/api/integration/v1/*`): tra digest có cache trong process
`CACHE_SECONDS` giây — thu hồi khoá / đổi phạm vi client có hiệu lực sau tối đa chừng đó; ghi
`last_used_at` thưa (`TOUCH_SECONDS`); giới hạn số request mỗi phút mỗi khoá
(`INTEGRATION_RATE_PER_MINUTE`, đếm trên Redis chính, cửa sổ phút); IP gửi khoá sai quá
`FAIL_LIMIT` lần trong `FAIL_WINDOW` giây bị 429. Redis lỗi thì cho qua (chỉ log): Redis trục trặc
không được chặn hệ thống ngoài.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from jobcrawler.common.logging import get_logger
from jobcrawler.database.models import ApiClient, ApiKey
from jobcrawler.services.resources import Resources

log = get_logger(__name__)

KEY_PREFIX = "jca_"
MAX_KEY_CHARS = 256
SHOWN_CHARS = 8  # ký tự đầu (sau tiền tố) lưu để nhận ra khoá
CACHE_SECONDS = 10.0
CACHE_MAX = 1024
TOUCH_SECONDS = 60.0
FAIL_LIMIT = 30
FAIL_WINDOW = 300

SCOPE_COMPANIES = "companies:write"
SCOPE_JOBS = "jobs:read"
SCOPES = (SCOPE_COMPANIES, SCOPE_JOBS)


def digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


@dataclass(frozen=True, slots=True)
class Principal:
    """Client đã xác thực của một request tích hợp (`request.state.api_client`)."""

    key_id: UUID
    client_id: UUID
    client_name: str
    scopes: frozenset[str]
    all_companies: bool
    include_description: bool

    @property
    def actor(self) -> str:
        """Người thao tác trong log / nhật ký thao tác."""
        return f"api:{self.client_name}"


async def create_key(
    session: AsyncSession, client_id: UUID, *, created_by: str
) -> tuple[ApiKey, str]:
    """Khoá mới cho client: (dòng, khoá — chỉ trả lần này)."""
    key = new_key()
    row = ApiKey(
        client_id=client_id,
        prefix=key[len(KEY_PREFIX) : len(KEY_PREFIX) + SHOWN_CHARS],
        digest=digest(key),
        created_by=created_by,
    )
    session.add(row)
    await session.flush()
    return row, key


async def lookup(session: AsyncSession, key: str) -> Principal | None:
    """Client của khoá còn hiệu lực, hoặc None."""
    if not key.startswith(KEY_PREFIX) or len(key) > MAX_KEY_CHARS:
        return None
    row = (
        await session.execute(
            select(ApiKey.id, ApiClient)
            .join(ApiClient, ApiClient.id == ApiKey.client_id)
            .where(ApiKey.digest == digest(key), ApiKey.revoked_at.is_(None))
        )
    ).first()
    if row is None:
        return None
    key_id, client = row
    return Principal(
        key_id=key_id,
        client_id=client.id,
        client_name=client.name,
        scopes=frozenset(client.scopes or ()),
        all_companies=client.all_companies,
        include_description=client.include_description,
    )


class ApiKeyAuth:
    """Xác thực khoá API cho GuardMiddleware (một bản mỗi process API)."""

    def __init__(self, res: Resources) -> None:
        self.res = res
        self._cache: dict[str, tuple[float, Principal | None]] = {}
        self._touched: dict[UUID, float] = {}

    @property
    def _prefix(self) -> str:
        return self.res.settings.redis_prefix

    async def verify(self, key: str) -> Principal | None:
        if not key.startswith(KEY_PREFIX) or len(key) > MAX_KEY_CHARS:
            return None
        h = digest(key)
        now = time.monotonic()
        hit = self._cache.get(h)
        if hit is not None and now - hit[0] < CACHE_SECONDS:
            principal = hit[1]
        else:
            async with self.res.sessionmaker() as session:
                principal = await lookup(session, key)
            if len(self._cache) >= CACHE_MAX:
                self._cache.clear()
            self._cache[h] = (now, principal)
        if principal is not None:
            await self._touch(principal.key_id, now)
        return principal

    def forget(self) -> None:
        """Bỏ cache (sau khi thu hồi khoá / đổi client ở chính process này)."""
        self._cache.clear()

    async def _touch(self, key_id: UUID, now: float) -> None:
        if now - self._touched.get(key_id, -TOUCH_SECONDS) < TOUCH_SECONDS:
            return
        self._touched[key_id] = now
        try:
            async with self.res.sessionmaker.begin() as session:
                await session.execute(
                    update(ApiKey).where(ApiKey.id == key_id).values(last_used_at=datetime.now(UTC))
                )
        except Exception as exc:  # chỉ là thông tin hiển thị: không làm hỏng request
            log.warning("integration.touch_failed", error=str(exc))

    async def rate_limited(self, key_id: UUID) -> int:
        """Số giây phải chờ nếu khoá đã dùng hết lượt của phút này, 0 nếu còn."""
        limit = self.res.settings.integration_rate_per_minute
        if limit <= 0:
            return 0
        window = int(time.time() // 60)
        name = f"{self._prefix}:integration:rate:{key_id}:{window}"
        try:
            count = int(await self.res.redis.incr(name))
            if count == 1:
                await self.res.redis.expire(name, 120)
        except Exception as exc:
            log.warning("integration.rate_unavailable", error=str(exc))
            return 0
        if count <= limit:
            return 0
        return max(1, 60 - int(time.time()) % 60)

    def _fail_key(self, ip: str) -> str:
        return f"{self._prefix}:integration:fail:{ip}"

    async def blocked(self, ip: str) -> int:
        """Số giây IP còn bị chặn vì gửi khoá sai nhiều lần (0: không bị chặn)."""
        try:
            raw = await self.res.redis.get(self._fail_key(ip))
            if raw is None or int(raw) < FAIL_LIMIT:
                return 0
            ttl = int(await self.res.redis.ttl(self._fail_key(ip)))
        except Exception as exc:
            log.warning("integration.throttle_unavailable", error=str(exc))
            return 0
        return max(ttl, 1)

    async def failed(self, ip: str) -> None:
        try:
            name = self._fail_key(ip)
            await self.res.redis.incr(name)
            await self.res.redis.expire(name, FAIL_WINDOW)
        except Exception as exc:
            log.warning("integration.throttle_unavailable", error=str(exc))
