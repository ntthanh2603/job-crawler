"""Mã hoá trường nhạy cảm lưu trong DB (khoá API LLM / SERP, danh sách proxy có mật khẩu của bảng
`app_settings`) bằng AES-256-GCM (`cryptography`).

Khoá: ENCRYPTION_KEYS (backend/.env) — một hoặc nhiều chuỗi bí mật cách nhau dấu phẩy; chuỗi CUỐI
dùng để mã hoá, các chuỗi trước chỉ để giải mã (đổi khoá: thêm khoá mới vào cuối, `jobcrawler
serve` mã hoá lại các giá trị cũ lúc khởi động rồi mới bỏ khoá cũ). Mỗi chuỗi → SHA-256 → khoá
32 byte; `kid` = 8 ký tự hex đầu của SHA-256 của khoá (biết bản mã dùng khoá nào mà không lộ khoá).
Không đặt: backend tự sinh một khoá lưu ở Redis chính (`<prefix>:secret:encryption_key`, dùng chung
các process) — mất Redis thì giá trị đã mã hoá không đọc lại được (phải nhập lại), nên production
đặt ENCRYPTION_KEYS. Khoá không bao giờ nằm trong Postgres cạnh bản mã.

Dạng lưu: `enc:v1:<kid>:<base64url(nonce 12 byte + bản mã + tag)>` của JSON của giá trị; dữ liệu
liên kết (AAD) là tên trường nên không chép được bản mã của trường này sang trường khác.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from collections.abc import Sequence
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from jobcrawler.config import Settings
from jobcrawler.logging import get_logger

log = get_logger(__name__)

PREFIX = "enc:v1:"
NONCE_BYTES = 12


class DecryptError(ValueError):
    """Bản mã hỏng, sai khoá hoặc khoá không còn trong ENCRYPTION_KEYS."""


def _kid(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:8]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def is_encrypted(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


class SecretBox:
    """Mã hoá / giải mã giá trị (JSON) gắn với tên trường."""

    def __init__(self, keys: Sequence[bytes]) -> None:
        if not keys:
            raise ValueError("cần ít nhất một khoá")
        self._ciphers = {_kid(k): AESGCM(k) for k in keys}
        self.active = _kid(keys[-1])

    @classmethod
    def from_secrets(cls, values: Sequence[str]) -> SecretBox:
        """Chuỗi bí mật (ENCRYPTION_KEYS) → khoá 32 byte (SHA-256)."""
        keys = [hashlib.sha256(v.strip().encode()).digest() for v in values if v.strip()]
        return cls(keys)

    def encrypt(self, field: str, value: Any) -> str:
        nonce = os.urandom(NONCE_BYTES)
        data = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
        sealed = self._ciphers[self.active].encrypt(nonce, data, field.encode())
        return f"{PREFIX}{self.active}:{_b64(nonce + sealed)}"

    def decrypt(self, field: str, token: str) -> Any:
        if not is_encrypted(token):
            raise DecryptError("không phải giá trị đã mã hoá")
        kid, _, body = token[len(PREFIX) :].partition(":")
        cipher = self._ciphers.get(kid)
        if cipher is None:
            raise DecryptError(f"không có khoá {kid} trong ENCRYPTION_KEYS")
        try:
            raw = _unb64(body)
            data = cipher.decrypt(raw[:NONCE_BYTES], raw[NONCE_BYTES:], field.encode())
        except (InvalidTag, ValueError) as exc:
            raise DecryptError("bản mã hỏng hoặc sai khoá") from exc
        return json.loads(data)

    def is_current(self, token: str) -> bool:
        """Bản mã dùng khoá đang hiệu lực (không cần mã hoá lại)."""
        return is_encrypted(token) and token[len(PREFIX) :].partition(":")[0] == self.active


_box: SecretBox | None = None


def configure(box: SecretBox | None) -> None:
    global _box
    _box = box


def current() -> SecretBox:
    if _box is None:
        raise RuntimeError("chưa có khoá mã hoá (secretbox.ensure chưa chạy)")
    return _box


def auto_key_name(prefix: str) -> str:
    return f"{prefix}:secret:encryption_key"


async def ensure(settings: Settings, redis: Any) -> SecretBox:
    """Dựng khoá của process: ENCRYPTION_KEYS, không có thì khoá tự sinh trên Redis chính."""
    raw = settings.encryption_keys.get_secret_value() if settings.encryption_keys else ""
    values = [v for v in raw.split(",") if v.strip()]
    if not values:
        key = auto_key_name(settings.redis_prefix)
        await redis.set(key, secrets.token_urlsafe(48), nx=True)
        stored = await redis.get(key)
        values = [stored.decode() if isinstance(stored, bytes) else str(stored)]
        log.warning(
            "secrets.auto_key",
            reason="chưa đặt ENCRYPTION_KEYS: dùng khoá mã hoá sinh ngẫu nhiên lưu trên Redis",
        )
    box = SecretBox.from_secrets(values)
    configure(box)
    return box
