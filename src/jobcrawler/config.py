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
    serp_results: int = Field(default=10, ge=1, le=20)
    serp_rate_per_second: float = Field(default=5.0, gt=0)
    # Cache kết quả tìm kiếm (giây) để chạy lại không tốn credit.
    serp_cache_seconds: int = Field(default=30 * 86_400, ge=0)
    serp_timeout_seconds: float = Field(default=15, gt=0)

    # KYC doanh nghiệp.
    # Kết quả KYC dùng lại trong chừng này ngày (`companies.kyc_at`).
    kyc_cache_days: float = Field(default=30, ge=0)
    # Ngưỡng pg_trgm similarity khi tra cache theo tên gần giống (chỉ khớp DN đã `verified`).
    kyc_trigram_threshold: float = Field(default=0.7, gt=0, le=1)
    # LLM phải chắc ít nhất mức này (cùng bằng chứng khớp mạnh) mới đánh `verified`.
    kyc_min_confidence: float = Field(default=0.8, ge=0, le=1)
    # Thời gian tối đa cho KYC trong một lượt (SERP + đọc website + LLM).
    kyc_timeout_seconds: float = Field(default=45, gt=0)
    # Số website ứng viên mở để đọc footer (tên pháp lý, MST, địa chỉ).
    kyc_max_sites: int = Field(default=3, ge=0)

    # Crawl và worker.
    # false = không dùng proxy (đi thẳng), kể cả khi PROXIES_FILE có proxy. CLI: --no-proxy.
    proxy_enabled: bool = True
    # Nằm trong thư mục config/ để compose mount cả thư mục (file chưa có cũng không lỗi).
    proxies_file: Path = Path("config/proxies.txt")
    worker_concurrency: int = Field(default=10, ge=1)
    browser_max_pages: int = Field(default=4, ge=1)
    # Tiền tố khoá Redis cho proxy pool, rate limit, robots, HTTP cache.
    redis_prefix: str = "jc"

    # HTTP. Impersonate Chrome; User-Agent do curl_cffi đặt khớp với fingerprint.
    http_impersonate: str = "chrome"
    http_timeout_seconds: float = Field(default=20, gt=0)
    http_max_retries: int = Field(default=3, ge=0)
    http_backoff_base_seconds: float = Field(default=1.0, ge=0)
    http_backoff_cap_seconds: float = Field(default=15.0, ge=0)
    # Retry-After lớn hơn mức này thì dừng nguồn (blocked) thay vì chờ.
    http_max_retry_after_seconds: float = Field(default=30.0, ge=0)
    http_cache_ttl_seconds: int = Field(default=3600, ge=0)
    http_accept_language: str = "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7"

    # Rate limit theo domain (token bucket trên Redis, dùng chung mọi process).
    rate_limit_per_second: float = Field(default=1.0, gt=0)
    rate_limit_burst: int = Field(default=1, ge=1)
    # Ghi đè theo domain, JSON: RATE_LIMITS='{"www.topcv.vn": 0.5}'.
    rate_limits: dict[str, float] = Field(default_factory=dict)

    # robots.txt.
    robots_cache_seconds: int = Field(default=86_400, ge=0)
    # Product token dùng để chọn nhóm luật trong robots.txt (ngoài "*").
    robots_user_agents: list[str] = Field(default_factory=lambda: ["*"])

    # Proxy pool.
    proxy_sticky_seconds: int = Field(default=1800, ge=1)
    proxy_cooldown_seconds: int = Field(default=300, ge=1)
    proxy_max_consecutive_failures: int = Field(default=5, ge=1)
    proxy_dead_seconds: int = Field(default=3600, ge=1)
    proxy_check_url: str = "https://www.cloudflare.com/cdn-cgi/trace"
    # Nguồn từ website doanh nghiệp (trang careers, ATS).
    discovery_cache_seconds: int = Field(default=7 * 86_400, ge=0)
    # Không tìm thấy nguồn nào: cache ngắn hơn để lượt sau thử lại sớm.
    discovery_negative_cache_seconds: int = Field(default=86_400, ge=0)
    # Thời gian tối đa cho bước tìm nguồn trong một lượt (phần còn lại để thu thập).
    discovery_timeout_seconds: float = Field(default=60, gt=0)
    source_max_jobs: int = Field(default=500, ge=1)
    source_max_detail_pages: int = Field(default=80, ge=0)
    source_max_listing_pages: int = Field(default=10, ge=1)
    source_max_rendered_pages: int = Field(default=10, ge=0)
    # Request/giây tới API công khai của ATS (host dùng chung cho mọi doanh nghiệp).
    # Ghi đè từng host bằng RATE_LIMITS.
    ats_rate_per_second: float = Field(default=2.0, gt=0)

    # Index các trang tuyển dụng (cron SAQ, `jobcrawler index-boards`).
    # Danh sách nguồn chạy (rỗng = mọi adapter đang bật), JSON: BOARDS='["topcv","itviec"]'.
    boards: list[str] = Field(default_factory=list)
    board_index_cron: str = "0 */5 * * *"  # mỗi 5 giờ
    # Mỗi lượt index tối đa chừng này giây / trang chi tiết; phần còn lại để lượt sau.
    board_index_budget_seconds: float = Field(default=4 * 3600, gt=0)
    board_max_jobs_per_run: int = Field(default=20_000, ge=1)
    # Tải lại tin không đổi `lastmod` sau chừng này ngày (kèm ETag/Last-Modified).
    board_refresh_days: float = Field(default=7, gt=0)
    board_concurrency: int = Field(default=4, ge=1)
    # process_company: index của profile cũ hơn chừng này giờ thì crawl trực tiếp trang profile.
    board_stale_hours: float = Field(default=24, gt=0)
    # Map profile ↔ doanh nghiệp: ngưỡng trigram để xét, độ tin cậy tối thiểu để nhận.
    profile_match_trigram: float = Field(default=0.6, gt=0, le=1)
    profile_match_min_confidence: float = Field(default=0.8, ge=0, le=1)
    # Doanh nghiệp chưa có profile trên một trang: tìm bằng SERP `site:` (cần SERP_API_KEY).
    board_serp_lookup: bool = True
    board_serp_lookup_seconds: float = Field(default=20, gt=0)

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
