"""Cấu hình ứng dụng, đọc từ biến môi trường và file `.env`."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # `LLM_BASE_URL=` trong .env nghĩa là "chưa cấu hình", không phải chuỗi rỗng.
        env_ignore_empty=True,
        extra="ignore",
    )

    # Kết nối. Mặc định khớp compose.yaml khi chạy CLI trên máy host.
    database_url: str = "postgresql+asyncpg://jobcrawler:jobcrawler@localhost:15432/jobcrawler"
    redis_url: str = "redis://localhost:16379/0"
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=10, ge=0)
    queue_name: str = "jobcrawler"

    # LLM: vLLM self-host (OpenAI-compatible). Để trống thì mọi bước dùng LLM bị tắt.
    llm_base_url: str | None = None
    llm_api_key: SecretStr = SecretStr("EMPTY")
    llm_model: str | None = None
    llm_disable_thinking: bool = True
    llm_max_concurrency: int = Field(default=8, ge=1)
    llm_max_input_chars: int = Field(default=20_000, ge=1_000)
    llm_timeout_seconds: float = Field(default=30, gt=0)

    # SERP. Thiếu key thì bỏ qua các bước dùng SERP (kèm cảnh báo).
    serp_provider: Literal["serper", "brave"] = "serper"
    serp_api_key: SecretStr | None = None

    # Crawl và worker.
    proxies_file: Path = Path("proxies.txt")
    worker_concurrency: int = Field(default=10, ge=1)
    browser_max_pages: int = Field(default=4, ge=1)
    # Deadline mềm: tới mốc này thì huỷ các nguồn chưa xong và lưu kết quả `partial`.
    company_deadline_seconds: float = Field(default=165, gt=0)
    # Timeout cứng của job SAQ `process_company`.
    company_timeout_seconds: int = Field(default=180, gt=0)

    # Log.
    log_level: LogLevel = "INFO"
    log_format: Literal["console", "json"] = "console"
    log_db_level: LogLevel = "INFO"

    @model_validator(mode="after")
    def _check_deadlines(self) -> Self:
        if self.company_deadline_seconds >= self.company_timeout_seconds:
            raise ValueError(
                "COMPANY_DEADLINE_SECONDS phải nhỏ hơn COMPANY_TIMEOUT_SECONDS "
                f"({self.company_deadline_seconds} >= {self.company_timeout_seconds})"
            )
        return self

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_base_url and self.llm_model)

    @property
    def serp_enabled(self) -> bool:
        return self.serp_api_key is not None


@lru_cache
def get_settings() -> Settings:
    return Settings()
