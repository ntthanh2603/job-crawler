"""Cấu hình worker, đọc từ biến môi trường, file `.env` và thư mục secret (xem worker/README.md).

Thứ tự ưu tiên: biến môi trường > `.env` > file trong thư mục secret > mặc định. Thư mục secret là
`SECRETS_DIR` (môi trường hoặc `.env`), không đặt thì `/run/secrets` nếu có (Docker / compose
`secrets:`); mỗi file một biến, tên file là tên biến (không phân biệt hoa thường, vd.
`gateway_token`), nội dung đã bỏ khoảng trắng / xuống dòng ở hai đầu.

Gateway điều chỉnh một phần lúc chạy (`WorkerConfig` trả về từ Register; số slot là trần, không
nâng được quá số của worker): xem `jobworker.client.RuntimeConfig`.
"""

from __future__ import annotations

import json
import secrets
import socket
import tempfile
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    NoDecode,
    PydanticBaseSettingsSource,
    SecretsSettingsSource,
    SettingsConfigDict,
    SettingsError,
)

from jobworker import sysres

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]

# Docker / compose `secrets:` mount mỗi secret thành một file ở đây.
DEFAULT_SECRETS_DIR = Path("/run/secrets")
# WORKER_WATCHDOG_SECONDS khác 0 thì ít nhất chừng này: hạn ngắn hơn giết cả worker khoẻ.
MIN_WATCHDOG_SECONDS = 10.0


def default_worker_id() -> str:
    """`<hostname>-<8 ký tự hex ngẫu nhiên>`: mỗi process một id, khởi động lại là id mới."""
    return f"{socket.gethostname()}-{secrets.token_hex(4)}"


def default_health_file() -> Path:
    """`<thư mục tạm>/jobworker-health.json`: `/tmp/jobworker-health.json` trong container."""
    return Path(tempfile.gettempdir()) / "jobworker-health.json"


def default_token_file() -> Path:
    """`~/.jobworker/token`: token riêng gateway cấp (giữ qua lần khởi động lại)."""
    return Path.home() / ".jobworker" / "token"


def split_proxies(value: Any) -> list[str]:
    """`WORKER_PROXIES`: JSON list, hoặc chuỗi cách nhau bởi dấu phẩy / xuống dòng. Phần sau `#`
    của mỗi dòng là chú thích (như file proxy): secret `worker_proxies` dùng được định dạng đó."""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            value = json.loads(text)
        else:
            lines = (line.split("#", 1)[0] for line in text.splitlines())
            return [p.strip() for line in lines for p in line.split(",") if p.strip()]
    if isinstance(value, list | tuple):
        return [str(p).strip() for p in value if str(p).strip()]
    raise ValueError("WORKER_PROXIES phải là JSON list hoặc chuỗi cách nhau bởi dấu phẩy")


def resolve_secrets_dir(*sources: PydanticBaseSettingsSource) -> Path | None:
    """`SECRETS_DIR` (lấy từ nguồn đầu tiên có: môi trường rồi `.env`); không đặt thì
    `/run/secrets` nếu thư mục đó có, không thì None (chạy trên máy dev: không đọc secret)."""
    for source in sources:
        if not isinstance(source, EnvSettingsSource):
            continue
        value = (source.env_vars.get("secrets_dir") or "").strip()
        if value:
            path = Path(value).expanduser()
            if not path.is_dir():
                # Đặt sai đường dẫn thì dừng luôn, không chạy tiếp với token rỗng.
                raise SettingsError(f"SECRETS_DIR={value} không phải thư mục")
            return path
    return DEFAULT_SECRETS_DIR if DEFAULT_SECRETS_DIR.is_dir() else None


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    # Gateway gRPC của backend.
    gateway_addr: str = "localhost:50051"
    # Khoá tham gia cố định (giống GATEWAY_TOKEN của backend): vào gateway lần đầu / khi token
    # riêng hết hạn (`jobworker.credentials`).
    gateway_token: SecretStr | None = None
    # Khoá tham gia dùng một lần (tạo ở trang Workers, hết hạn sau 5 phút).
    worker_join_key: SecretStr | None = None
    # Nơi lưu token riêng gateway cấp; mount volume vào đây để worker tạo lại vẫn giữ token.
    worker_token_file: Path = Field(default_factory=default_token_file)
    gateway_tls: bool = False
    # CA riêng (PEM) khi gateway dùng chứng chỉ tự ký; trống = CA hệ thống.
    gateway_ca_cert: Path | None = None

    worker_id: str = Field(default_factory=default_worker_id)
    # Nhóm worker (backend định tuyến domain → pool qua FETCH_ROUTES).
    worker_pool: str = "default"
    # Trống = tự tính theo CPU / RAM của máy (`jobworker.sysres`, như open-asm); đặt số thì dùng số
    # đó. Dùng `http_slots` / `render_slots` (đã tính) thay vì đọc thẳng hai field này.
    worker_http_slots: int | None = Field(default=None, ge=1)
    # 0 = không render: không bao giờ khởi động Chromium (gateway cũng không bật lên được).
    worker_render_slots: int | None = Field(default=None, ge=0)
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

    # File trạng thái cho `jobworker health` (healthcheck của Docker), xem jobworker.health.
    worker_health_file: Path = Field(default_factory=default_health_file)
    # Watchdog (jobworker.watchdog): event loop hoặc vòng heartbeat đứng quá chừng này giây thì
    # thoát mã 70 để Docker (`restart: unless-stopped`) chạy lại; 0 = tắt.
    worker_watchdog_seconds: float = Field(default=120, ge=0)

    @property
    def http_slots(self) -> int:
        return self.worker_http_slots or sysres.auto_http_slots()

    @property
    def render_slots(self) -> int:
        if self.worker_render_slots is not None:
            return self.worker_render_slots
        return sysres.auto_render_slots()

    log_level: LogLevel = "INFO"
    log_format: Literal["console", "json"] = "console"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Thư mục secret quyết định lúc tạo settings (không cố định trong model_config) để đọc
        # được SECRETS_DIR; `_secrets_dir=` truyền tay (test) thì giữ nguyên.
        if isinstance(file_secret_settings, SecretsSettingsSource) and (
            file_secret_settings.secrets_dir is None
        ):
            file_secret_settings.secrets_dir = resolve_secrets_dir(env_settings, dotenv_settings)
        return init_settings, env_settings, dotenv_settings, file_secret_settings

    @field_validator("worker_proxies", mode="before")
    @classmethod
    def _split_proxies(cls, value: Any) -> list[str]:
        return split_proxies(value)

    @field_validator("worker_watchdog_seconds")
    @classmethod
    def _watchdog_min(cls, value: float) -> float:
        if 0 < value < MIN_WATCHDOG_SECONDS:
            raise ValueError(f"0 (tắt) hoặc ít nhất {MIN_WATCHDOG_SECONDS:.0f} giây")
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_level(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value
