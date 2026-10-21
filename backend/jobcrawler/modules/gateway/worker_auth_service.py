"""Xác thực worker lấy trang (theo cách của open-asm, nhưng chỉ nằm trên Redis, không lưu DB).

Bật khi đặt GATEWAY_TOKEN; trống thì gateway không hỏi gì (dev, mạng nội bộ).

1. **Vào lần đầu** (`Register`, metadata `authorization: Bearer <khoá>`), một trong hai:
   - GATEWAY_TOKEN — khoá cố định cho worker trong hạ tầng của mình (compose, máy worker riêng);
   - khoá tham gia dùng MỘT lần tạo ở trang Workers (`create_join_key`): sống JOIN_TTL_SECONDS
     (15 phút) trên Redis, xoá lúc dùng nên hai worker không dùng chung được.
   Gateway trả `worker_token` riêng của worker (`RegisterResponse.worker_token`).
2. **Mọi lời gọi sau** (Lease / Complete / Heartbeat / Leave, cả Register lại): token riêng đó.
   Token gắn với `worker_id` hiện tại (đổi khi worker khởi động lại và Register bằng token cũ);
   gọi với `worker_id` khác bị từ chối. Token hết hạn sau WORKER_TOKEN_TTL_SECONDS không dùng (mỗi
   Heartbeat gia hạn); hết hạn / mất Redis thì worker vào lại bằng GATEWAY_TOKEN (khoá tham gia đã
   dùng thì cần khoá mới).
3. **Một worker_id đang sống chỉ thuộc một token**: Register (bằng khoá nào cũng vậy) với id
   đang sống dưới token khác bị từ chối (`WorkerIdBusyError`, gateway trả ALREADY_EXISTS, worker
   giữ khoá và thử lại tới khi bản cũ hết hạn báo sống). Không thì worker vào bằng khoá tham gia
   đăng ký trùng id của worker khác rồi Leave (việc nó đang giữ thành `worker_lost`) hay Complete
   kết quả giả. Worker khởi động lại với cùng id và token đã lưu vẫn vào ngay.

Register là MỘT script Lua (`_REGISTER`): kiểm khoá tham gia / token, giành worker_id, dùng khoá
tham gia + đánh dấu đã dùng, ghi token — lỗi Redis giữa chừng không làm mất khoá tham gia.

Redis chỉ giữ SHA-256 của khoá / token (dump Redis không lộ khoá dùng được):

- `<prefix>:gw:join:<sha256(khoá)>` → JSON {id, created_by, created_at} (TTL 15 phút);
- `<prefix>:gw:join-id:<id>` → sha256 của khoá (để huỷ theo id mà không cần khoá), sau khi dùng là
  `used:<worker_id>` (giao diện biết khoá đã được dùng; huỷ không xoá dấu này);
- `<prefix>:gw:wtoken:<sha256(token)>` → JSON {worker_id, via, issued_at} (TTL trượt);
- `<prefix>:gw:owner:<worker_id>` → sha256 của token Register gần nhất với id đó (TTL trượt như
  token, Heartbeat gia hạn).
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

JOIN_TTL_SECONDS = 900
WORKER_TOKEN_TTL_SECONDS = 7 * 24 * 3600
MAX_CREDENTIAL_CHARS = 256
JOIN_PREFIX = "jcj_"
TOKEN_PREFIX = "jcw_"

JoinStatus = Literal["pending", "used", "expired"]

# Register nguyên tử. KEYS: workers (hạn báo sống của registry), owner:<id>, wtoken:<sha(token)>,
# [join:<sha(khoá)>]. ARGV: worker_id, sha(token), JSON bản ghi token, TTL token (s), cách vào
# (static | token | join), `<prefix>:gw`, TTL dấu đã dùng (s). Trả {'invalid'} (khoá / token không
# còn), {'busy'} (id đang sống dưới token khác: chưa đụng gì) hoặc {'ok', id khoá tham gia | ''}.
_REGISTER = """
local raw
if ARGV[5] == 'join' then
  raw = redis.call('GET', KEYS[4])
  if not raw then return {'invalid'} end
elseif ARGV[5] == 'token' then
  if redis.call('EXISTS', KEYS[3]) == 0 then return {'invalid'} end
end
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local score = redis.call('ZSCORE', KEYS[1], ARGV[1])
local owner = redis.call('GET', KEYS[2])
if score and tonumber(score) > now and owner and owner ~= ARGV[2] then
  return {'busy'}
end
local join_id = ''
if raw then
  join_id = tostring(cjson.decode(raw)['id'])
  redis.call('DEL', KEYS[4])
  redis.call('SET', ARGV[6] .. ':join-id:' .. join_id, 'used:' .. ARGV[1], 'EX', ARGV[7])
end
redis.call('SET', KEYS[3], ARGV[3], 'EX', ARGV[4])
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[4])
return {'ok', join_id}
"""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


class WorkerIdBusyError(Exception):
    """worker_id đang sống dưới token khác: Register bị từ chối (vào được khi bản cũ hết hạn)."""


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


@dataclass(frozen=True, slots=True)
class Registration:
    via: str  # "static" | "token" (token riêng đang dùng) | "join"
    token: str | None  # token mới cấp; None = worker giữ token đang dùng
    join_id: str | None = None


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

    def _owner_key(self, worker_id: str) -> str:
        return f"{self.base}:owner:{worker_id}"

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
        """Huỷ khoá chưa dùng. False khi không còn gì để huỷ: hết hạn / đã huỷ, hoặc worker đã dùng
        khoá (giữ dấu `used:` để giao diện thấy worker đã vào)."""
        digest = await self.redis.get(self._join_id(join_id))
        if digest is None:
            return False
        text = _text(digest)
        # Xoá khoá trước và xét số khoá xoá được: worker dùng khoá giữa GET và DEL (Register xoá
        # khoá và ghi dấu `used:` trong một bước) thì không xoá dấu đó, không báo nhầm là đã huỷ.
        if text.startswith("used:") or not await self.redis.delete(self._join_key(text)):
            return False
        await self.redis.delete(self._join_id(join_id))
        log.info("gateway.join_key_revoked", join_id=join_id)
        return True

    async def join_key_status(self, join_id: str) -> tuple[JoinStatus, str | None]:
        """(trạng thái, worker_id đã dùng khoá)."""
        value = await self.redis.get(self._join_id(join_id))
        if value is None:
            return "expired", None
        text = _text(value)
        if text.startswith("used:"):
            return "used", text.removeprefix("used:")
        return "pending", None

    # --- Register / token của worker -------------------------------------------------------

    async def register(
        self, credential: str, *, worker_id: str, workers: str
    ) -> Registration | None:
        """Register bằng `credential` (GATEWAY_TOKEN, token riêng, khoá tham gia), nguyên tử
        (`_REGISTER`): giành `worker_id` cho token của worker, dùng khoá tham gia, ghi token.
        `workers`: sorted set hạn báo sống (`Keys.workers`). None: khoá sai / hết hạn / đã dùng.
        `WorkerIdBusyError`: id đang sống dưới token khác (khoá tham gia chưa bị dùng)."""
        mode = "token"
        record = None
        if self.is_static(credential):
            mode = "static"
        elif (record := await self.check_token(credential)) is None:
            if not credential.startswith(JOIN_PREFIX) or len(credential) > MAX_CREDENTIAL_CHARS:
                return None
            mode = "join"
        # Token cũ (worker khởi động lại với worker_id mới): giữ token, gắn với id mới.
        token = credential if record is not None else TOKEN_PREFIX + secrets.token_urlsafe(32)
        data = {
            "worker_id": worker_id,
            "via": record.via if record is not None else mode,
            "issued_at": record.issued_at_ms if record is not None else int(time.time() * 1000),
        }
        keys = [workers, self._owner_key(worker_id), self._token_key(token)]
        if mode == "join":
            keys.append(self._join_key(_sha(credential)))
        found: Any = await self.redis.eval(
            _REGISTER,
            len(keys),
            *keys,
            worker_id,
            _sha(token),
            json.dumps(data),
            self.token_ttl,
            mode,
            self.base,
            JOIN_TTL_SECONDS,
        )
        status = _text(found[0])
        if status == "invalid":
            return None
        if status == "busy":
            raise WorkerIdBusyError(worker_id)
        return Registration(mode, None if mode == "token" else token, _text(found[1]) or None)

    async def check_token(self, token: str) -> TokenRecord | None:
        if not token.startswith(TOKEN_PREFIX) or len(token) > MAX_CREDENTIAL_CHARS:
            return None
        raw = await self.redis.get(self._token_key(token))
        if raw is None:
            return None
        data = json.loads(raw)
        return TokenRecord(str(data["worker_id"]), str(data["via"]), int(data["issued_at"]))

    async def touch(self, token: str, worker_id: str) -> None:
        """Gia hạn token và quyền giữ `worker_id` (Heartbeat): worker còn chạy thì không hết hạn.
        Id chưa có chủ (đăng ký trước bản gateway này) thì thuộc token này."""
        owner = self._owner_key(worker_id)
        pipe = self.redis.pipeline(transaction=False)
        pipe.expire(self._token_key(token), self.token_ttl)
        pipe.set(owner, _sha(token), nx=True, ex=self.token_ttl)
        pipe.expire(owner, self.token_ttl)
        await pipe.execute()


def bearer(metadata: Any) -> str | None:
    """Giá trị `authorization: Bearer <...>` của metadata gRPC."""
    for key, value in metadata or ():
        if key.lower() == "authorization":
            text = value.decode() if isinstance(value, bytes) else str(value)
            scheme, _, credential = text.partition(" ")
            if scheme.lower() == "bearer" and credential.strip():
                return credential.strip()[: MAX_CREDENTIAL_CHARS + 1]
    return None
