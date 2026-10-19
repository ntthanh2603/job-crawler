"""Cấu hình worker, đọc từ biến môi trường và file `.env` (xem worker/README.md).

Gateway điều chỉnh một phần lúc chạy (`WorkerConfig` trả về từ Register; số slot là trần, không
nâng được quá số của worker): xem `jobworker.client.RuntimeConfig`.
"""

from __future__ import annotations

import json
import secrets
import socket
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


def default_worker_id() -> str:
    """`<hostname>-<8 ký tự hex ngẫu nhiên>`: mỗi process một id, khởi động lại là id mới."""
    return f"{socket.gethostname()}-{secrets.token_hex(4)}"


def split_proxies(value: Any) -> list[str]:
    """`WORKER_PROXIES`: JSON list, hoặc chuỗi cách nhau bởi dấu phẩy / xuống dòng."""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            value = json.loads(text)
        else:
            return [p.strip() for p in text.replace("\n", ",").split(",") if p.strip()]
    if isinstance(value, list | tuple):
        return [str(p).strip() for p in value if str(p).strip()]
    raise ValueError("WORKER_PROXIES phải là JSON list hoặc chuỗi cách nhau bởi dấu phẩy")


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    # Gateway gRPC của backend.
    gateway_addr: str = "localhost:50051"
    # Gửi kèm mỗi lời gọi: metadata `authorization: Bearer <token>`.
    gateway_token: SecretStr | None = None
    gateway_tls: bool = False
    # CA riêng (PEM) khi gateway dùng chứng chỉ tự ký; trống = CA hệ thống.
    gateway_ca_cert: Path | None = None

    worker_id: str = Field(default_factory=default_worker_id)
    # Nhóm worker (backend định tuyến domain → pool qua FETCH_ROUTES).
    worker_pool: str = "default"
    worker_http_slots: int = Field(default=32, ge=1)
    # 0 = không render: không bao giờ khởi động Chromium (gateway cũng không bật lên được).
    worker_render_slots: int = Field(default=2, ge=0)
    # Proxy tại worker (định dạng như config/proxies.example.txt).
    worker_proxies: Annotated[list[str], NoDecode] = Field(default_factory=list)
    worker_proxies_file: Path | None = None
    worker_impersonate: str = "chrome"
    worker_labels: dict[str, str] = Field(default_factory=dict)
    worker_max_body_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    # SIGTERM / Ctrl-C: chờ việc đang làm tối đa chừng này giây rồi mới thoát.
    worker_shutdown_grace_seconds: float = Field(default=30, ge=0)
    # Proxy tại worker: giữ một proxy cho mỗi domain chừng này giây; nghỉ sau lỗi / 403 / 429.
    worker_proxy_sticky_seconds: float = Field(default=600, gt=0)
    worker_proxy_cooldown_seconds: float = Field(default=120, gt=0)

    log_level: LogLevel = "INFO"
    log_format: Literal["console", "json"] = "console"

    @field_validator("worker_proxies", mode="before")
    @classmethod
    def _split_proxies(cls, value: Any) -> list[str]:
        return split_proxies(value)

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_level(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value
