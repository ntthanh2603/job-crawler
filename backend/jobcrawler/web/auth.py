"""Đăng nhập giao diện / API bằng tài khoản trong `.env` (UI_USERNAME / UI_PASSWORD) và JWT.

- `POST /api/auth/login`: đúng tài khoản → JWT (HS256: `sub` = tên đăng nhập, `jti`, `iat`,
  `exp` sau AUTH_TOKEN_HOURS giờ, `ver` = dấu của mật khẩu) đặt vào cookie httpOnly `jc_token`
  (SameSite=Lax, Secure khi HTTPS) và trả trong body cho script (`Authorization: Bearer <token>`).
- Mỗi request (`GuardMiddleware`): token từ cookie hoặc header Bearer; đúng chữ ký, còn hạn, `sub`
  khớp UI_USERNAME và `ver` khớp mật khẩu hiện tại (đổi tài khoản / mật khẩu trong `.env` là mọi
  token cũ hết dùng), `jti` chưa bị thu hồi.
- `POST /api/auth/logout`: thu hồi `jti` (khoá Redis sống tới khi token hết hạn) và xoá cookie.
- Khoá ký: JWT_SECRET; trống thì tạo một khoá ngẫu nhiên lưu ở Redis chính
  (`<prefix>:auth:jwt_secret`, dùng chung mọi process; mất Redis thì mọi người đăng nhập lại).
- Đoán mật khẩu: `LoginThrottle` (sai 20 lần / 5 phút / IP → 429).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any

import jwt

from jobcrawler.config import Settings
from jobcrawler.logging import get_logger

log = get_logger(__name__)

COOKIE = "jc_token"
ALGORITHM = "HS256"


@dataclass(frozen=True, slots=True)
class Claims:
    user: str
    jti: str
    expires_at: int  # epoch giây


@dataclass(frozen=True, slots=True)
class Issued:
    token: str
    jti: str
    expires_at: int


class TokenAuth:
    """Ký / kiểm JWT của tài khoản duy nhất trong `.env`."""

    def __init__(self, *, username: str, password: str, secret: bytes, hours: float) -> None:
        self.username = username
        self._password = password
        self._secret = secret
        self.ttl = max(60, int(hours * 3600))
        # Dấu của mật khẩu (không lộ mật khẩu): đổi mật khẩu thì token cũ hết hợp lệ.
        self._ver = hmac.new(secret, password.encode(), hashlib.sha256).hexdigest()[:16]

    def check_password(self, username: str, password: str) -> bool:
        user_ok = secrets.compare_digest(username.encode(), self.username.encode())
        pass_ok = secrets.compare_digest(password.encode(), self._password.encode())
        return user_ok & pass_ok

    def issue(self, *, now: float | None = None) -> Issued:
        now_s = int(now if now is not None else time.time())
        jti = uuid.uuid4().hex
        exp = now_s + self.ttl
        payload = {"sub": self.username, "jti": jti, "iat": now_s, "exp": exp, "ver": self._ver}
        return Issued(jwt.encode(payload, self._secret, algorithm=ALGORITHM), jti, exp)

    def verify(self, token: str) -> Claims | None:
        """Claims của token hợp lệ (chưa xét thu hồi); None nếu sai / hết hạn / khác tài khoản."""
        try:
            data = jwt.decode(
                token,
                self._secret,
                algorithms=[ALGORITHM],
                options={"require": ["sub", "jti", "exp", "ver"]},
            )
        except jwt.PyJWTError:
            return None
        if data.get("sub") != self.username or data.get("ver") != self._ver:
            return None
        return Claims(user=str(data["sub"]), jti=str(data["jti"]), expires_at=int(data["exp"]))


def secret_key(prefix: str) -> str:
    return f"{prefix}:auth:jwt_secret"


def revoked_key(prefix: str, jti: str) -> str:
    return f"{prefix}:auth:revoked:{jti}"


async def resolve_secret(settings: Settings, redis: Any) -> bytes:
    """JWT_SECRET; trống thì khoá ngẫu nhiên dùng chung trên Redis (tạo một lần)."""
    if settings.jwt_secret is not None and settings.jwt_secret.get_secret_value():
        return settings.jwt_secret.get_secret_value().encode()
    key = secret_key(settings.redis_prefix)
    await redis.set(key, secrets.token_urlsafe(48), nx=True)
    value = await redis.get(key)
    log.warning(
        "auth.jwt_secret_generated",
        reason="chưa đặt JWT_SECRET: dùng khoá sinh ngẫu nhiên lưu trên Redis",
    )
    return value if isinstance(value, bytes) else str(value).encode()


async def revoke(redis: Any, prefix: str, claims: Claims, *, now: float | None = None) -> None:
    ttl = claims.expires_at - int(now if now is not None else time.time())
    if ttl > 0:
        await redis.set(revoked_key(prefix, claims.jti), 1, ex=ttl)


async def is_revoked(redis: Any, prefix: str, jti: str) -> bool:
    return bool(await redis.exists(revoked_key(prefix, jti)))


def token_from(headers: Any) -> str | None:
    """Token từ header `Authorization: Bearer …`, không có thì từ cookie `jc_token`."""
    scheme, _, value = (headers.get("authorization") or "").partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    for part in (headers.get("cookie") or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE and value:
            return value
    return None
