"""Khoá của worker với gateway (gateway bật xác thực khi backend đặt GATEWAY_TOKEN).

- **Token riêng** (`jcw_…`): gateway cấp khi Register bằng khoá tham gia; dùng cho mọi lời gọi
  sau, lưu ở WORKER_TOKEN_FILE (quyền 0600) để khởi động lại không cần khoá mới. Gateway từ chối
  (hết hạn, mất Redis) thì bỏ token, vào lại bằng khoá tham gia.
- **Khoá tham gia**, thử lần lượt: WORKER_JOIN_KEY (một lần, tạo ở trang Workers, hết hạn sau 5
  phút) rồi GATEWAY_TOKEN (cố định, cho worker trong hạ tầng của mình).

Gateway không bật xác thực thì gửi gì cũng được (khoá bị bỏ qua, không cấp token).
"""

from __future__ import annotations

import contextlib
import os

from jobworker.common.config import WorkerSettings
from jobworker.common.logging import get_logger

log = get_logger(__name__)

Metadata = tuple[tuple[str, str], ...]


def _bearer(value: str | None) -> Metadata:
    return (("authorization", f"Bearer {value}"),) if value else ()


class Credentials:
    def __init__(self, settings: WorkerSettings) -> None:
        self.path = settings.worker_token_file
        self.token = self._read()
        keys = [
            s.get_secret_value().strip()
            for s in (settings.worker_join_key, settings.gateway_token)
            if s is not None
        ]
        self.join_keys = list(dict.fromkeys(k for k in keys if k))
        self._join = 0

    def _read(self) -> str | None:
        try:
            text = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return text or None

    @property
    def join_key(self) -> str | None:
        return self.join_keys[self._join] if self._join < len(self.join_keys) else None

    def metadata(self) -> Metadata:
        """Lời gọi thường: token riêng; chưa có (gateway không bật xác thực) thì khoá tham gia."""
        return _bearer(self.token or self.join_key)

    def accept(self, token: str) -> None:
        """Token gateway vừa cấp: dùng từ giờ và lưu lại (lưu lỗi thì chỉ giữ trong bộ nhớ)."""
        self.token = token
        self._join = 0
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(token)
        except OSError as exc:
            log.warning("worker.token_not_saved", path=str(self.path), error=str(exc))

    def reject(self) -> bool:
        """Gateway từ chối khoá vừa dùng. Trả True nếu còn khoá khác để thử ngay."""
        if self.token is not None:
            log.warning("worker.token_rejected", reason="token hết hạn hoặc bị thu hồi")
            self.token = None
            with contextlib.suppress(OSError):
                self.path.unlink()
            return bool(self.join_keys)
        self._join += 1
        if self._join < len(self.join_keys):
            return True
        self._join = 0  # hết khoá: lần sau (sau khi chờ) thử lại từ đầu
        return False
