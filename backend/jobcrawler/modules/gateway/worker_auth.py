"""Xác thực worker lấy trang (theo cách của open-asm, nhưng chỉ nằm trên Redis, không lưu DB).

Bật khi đặt GATEWAY_TOKEN; trống thì gateway không hỏi gì (dev, mạng nội bộ).

1. **Vào lần đầu** (`Register`, metadata `authorization: Bearer <khoá>`), một trong hai:
   - GATEWAY_TOKEN — khoá cố định cho worker trong hạ tầng của mình (compose, máy worker riêng);
   - khoá tham gia dùng MỘT lần tạo ở trang Workers (`create_join_key`): sống JOIN_TTL_SECONDS
     (5 phút) trên Redis, `GETDEL` lúc dùng nên hai worker không dùng chung được.
   Gateway trả `worker_token` riêng của worker (`RegisterResponse.worker_token`).
2. **Mọi lời gọi sau** (Lease / Complete / Heartbeat / Leave, cả Register lại): token riêng đó.
   Token gắn với `worker_id` hiện tại (đổi khi worker khởi động lại và Register bằng token cũ);
   gọi với `worker_id` khác bị từ chối. Token hết hạn sau WORKER_TOKEN_TTL_SECONDS không dùng (mỗi
   Heartbeat gia hạn); hết hạn / mất Redis thì worker vào lại bằng GATEWAY_TOKEN (khoá tham gia đã
   dùng thì cần khoá mới).

Redis chỉ giữ SHA-256 của khoá / token (dump Redis không lộ khoá dùng được):

- `<prefix>:gw:join:<sha256(khoá)>` → JSON {id, created_by, created_at} (TTL 5 phút);
- `<prefix>:gw:join-id:<id>` → sha256 của khoá (để huỷ theo id mà không cần khoá), sau khi dùng là
  `used:<worker_id>` (giao diện biết khoá đã được dùng);
- `<prefix>:gw:wtoken:<sha256(token)>` → JSON {worker_id, via, issued_at} (TTL trượt).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from jobcrawler.common.logging import get_logger

log = get_logger(__name__)

JOIN_TTL_SECONDS = 300
WORKER_TOKEN_TTL_SECONDS = 7 * 24 * 3600
MAX_CREDENTIAL_CHARS = 256
JOIN_PREFIX = "jcj_"
TOKEN_PREFIX = "jcw_"

JoinStatus = Literal["pending", "used", "expired"]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class JoinKey:
    id: str
    key: str  # chỉ trả một lần lúc tạo
    expires_at_ms: int


@dataclass(frozen=True, slots=True)
class TokenRecord:
    worker_id: str
    via: str  # "static" (GATEWAY_TOKEN) | "join" (khoá tham gia)
    issued_at_ms: int


class WorkerAuth:
    def __init__(
        self,
        redis: Any,
        base: str,
        *,
        static_token: str | None,
        token_ttl: int = WORKER_TOKEN_TTL_SECONDS,
    ) -> None:
        self.redis = redis
        self.base = base  # `<prefix>:gw`
        self._static = static_token or None
        self.token_ttl = token_ttl

    @property
    def enabled(self) -> bool:
        return self._static is not None

    def _join_key(self, digest: str) -> str:
        return f"{self.base}:join:{digest}"

    def _join_id(self, join_id: str) -> str:
        return f"{self.base}:join-id:{join_id}"

    def _token_key(self, token: str) -> str:
        return f"{self.base}:wtoken:{_sha(token)}"

    def is_static(self, credential: str) -> bool:
        return self._static is not None and hmac.compare_digest(
            credential.encode(), self._static.encode()
        )

    # --- khoá tham gia ---------------------------------------------------------------------

    async def create_join_key(self, *, created_by: str) -> JoinKey:
        join_id = uuid.uuid4().hex[:12]
        key = JOIN_PREFIX + secrets.token_urlsafe(24)
        now_ms = int(time.time() * 1000)
        record = json.dumps({"id": join_id, "created_by": created_by, "created_at": now_ms})
        digest = _sha(key)
        pipe = self.redis.pipeline()
        pipe.set(self._join_key(digest), record, ex=JOIN_TTL_SECONDS)
        pipe.set(self._join_id(join_id), digest, ex=JOIN_TTL_SECONDS)
        await pipe.execute()
        log.info("gateway.join_key_created", join_id=join_id, created_by=created_by)
        return JoinKey(join_id, key, now_ms + JOIN_TTL_SECONDS * 1000)

    async def revoke_join_key(self, join_id: str) -> bool:
        digest = await self.redis.get(self._join_id(join_id))
        if digest is None:
            return False
        text = digest.decode() if isinstance(digest, bytes) else str(digest)
        names = (
            [self._join_id(join_id)]
            if text.startswith("used:")
            else [
                self._join_id(join_id),
                self._join_key(text),
            ]
        )
        await self.redis.delete(*names)
        log.info("gateway.join_key_revoked", join_id=join_id)
        return True

    async def join_key_status(self, join_id: str) -> tuple[JoinStatus, str | None]:
        """(trạng thái, worker_id đã dùng khoá)."""
        value = await self.redis.get(self._join_id(join_id))
        if value is None:
            return "expired", None
        text = value.decode() if isinstance(value, bytes) else str(value)
        if text.startswith("used:"):
            return "used", text.removeprefix("used:")
        return "pending", None

    async def consume_join_key(self, key: str, *, worker_id: str) -> str | None:
        """Dùng khoá tham gia (một lần, nguyên tử): id khoá, None nếu sai / hết hạn / đã dùng."""
        if not key.startswith(JOIN_PREFIX) or len(key) > MAX_CREDENTIAL_CHARS:
            return None
        raw = await self.redis.getdel(self._join_key(_sha(key)))
        if raw is None:
            return None
        join_id = str(json.loads(raw)["id"])
        await self.redis.set(self._join_id(join_id), f"used:{worker_id}", ex=JOIN_TTL_SECONDS)
        return join_id

    # --- token của worker ------------------------------------------------------------------

    async def issue_token(self, *, worker_id: str, via: str) -> str:
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        record = {"worker_id": worker_id, "via": via, "issued_at": int(time.time() * 1000)}
        await self.redis.set(self._token_key(token), json.dumps(record), ex=self.token_ttl)
        return token

    async def check_token(self, token: str) -> TokenRecord | None:
        if not token.startswith(TOKEN_PREFIX) or len(token) > MAX_CREDENTIAL_CHARS:
            return None
        raw = await self.redis.get(self._token_key(token))
        if raw is None:
            return None
        data = json.loads(raw)
        return TokenRecord(str(data["worker_id"]), str(data["via"]), int(data["issued_at"]))

    async def bind(self, token: str, record: TokenRecord, worker_id: str) -> None:
        """Worker khởi động lại (worker_id mới) và Register bằng token cũ: gắn token với id mới."""
        data = {"worker_id": worker_id, "via": record.via, "issued_at": record.issued_at_ms}
        await self.redis.set(self._token_key(token), json.dumps(data), ex=self.token_ttl)

    async def touch(self, token: str) -> None:
        """Gia hạn token (Heartbeat): worker còn chạy thì token không hết hạn."""
        await self.redis.expire(self._token_key(token), self.token_ttl)


def bearer(metadata: Any) -> str | None:
    """Giá trị `authorization: Bearer <...>` của metadata gRPC."""
    for key, value in metadata or ():
        if key.lower() == "authorization":
            text = value.decode() if isinstance(value, bytes) else str(value)
            scheme, _, credential = text.partition(" ")
            if scheme.lower() == "bearer" and credential.strip():
                return credential.strip()[: MAX_CREDENTIAL_CHARS + 1]
    return None
