"""Cấu hình ứng dụng, đọc từ biến môi trường, file `.env` và file bí mật (Docker secrets).

Thứ tự ưu tiên (của pydantic-settings, cao → thấp): tham số truyền vào `Settings(...)` → biến môi
trường → `.env` → file bí mật trong `SECRETS_DIR` → mặc định trong mã. Biến môi trường đặt rỗng
(`GATEWAY_TOKEN=`) bị bỏ qua (`env_ignore_empty`), nên file bí mật cùng tên vẫn được dùng.

File bí mật: mỗi file một giá trị, tên file = tên field (không phân biệt hoa thường), vd.
`/run/secrets/gateway_token`, `llm_api_key`, `serp_api_key`, `ui_password`,
`database_url`; nội dung được bỏ khoảng trắng / xuống dòng ở hai đầu. Thư mục lấy từ biến môi
trường `SECRETS_DIR` (đặt rỗng = tắt); không đặt thì dùng `/run/secrets` nếu thư mục đó tồn tại
(chỗ Docker / compose gắn secrets), nên chạy trên máy dev không bị ảnh hưởng.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SecretsSettingsSource,
    SettingsConfigDict,
)

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
AuthMode = Literal["proxy", "jwt", "none"]
_WINDOW = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")
# Chỗ Docker / compose gắn secrets (`secrets:` của service).
DEFAULT_SECRETS_DIR = Path("/run/secrets")


def secrets_dir() -> Path | None:
    """Thư mục file bí mật: `SECRETS_DIR` (rỗng = tắt), không đặt thì `/run/secrets` nếu có."""
    raw = os.environ.get("SECRETS_DIR")
    if raw is not None:
        return Path(raw.strip()) if raw.strip() else None
    return DEFAULT_SECRETS_DIR if DEFAULT_SECRETS_DIR.is_dir() else None


def parse_window(value: str, name: str = "window") -> tuple[int, int]:
    """Khung giờ "HH:MM-HH:MM" → (phút bắt đầu, phút kết thúc) trong ngày; "24:00" là cuối ngày.
    Bắt đầu = kết thúc nghĩa là cả ngày."""
    m = _WINDOW.match(value)
    if m is None:
        raise ValueError(f"{name} phải có dạng HH:MM-HH:MM, nhận {value!r}")
    h1, m1, h2, m2 = (int(g) for g in m.groups())
    start, end = h1 * 60 + m1, h2 * 60 + m2
    if m1 >= 60 or m2 >= 60 or start > 24 * 60 or end > 24 * 60:
        raise ValueError(f"{name}: giờ không hợp lệ {value!r}")
    return start % (24 * 60), end


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # `LLM_BASE_URL=` trong .env nghĩa là "chưa cấu hình", không phải chuỗi rỗng.
        env_ignore_empty=True,
        extra="ignore",
    )

    # Kết nối. Mặc định khớp docker-compose.yaml khi chạy CLI trên máy host.
    # 127.0.0.1 chứ không localhost: compose chỉ mở cổng ở IPv4 loopback, và trên Windows localhost
    # thử ::1 trước (mỗi kết nối mới chờ thêm ~2 s).
    database_url: str = "postgresql+asyncpg://jobcrawler:jobcrawler@127.0.0.1:15432/jobcrawler"
    # Redis chính: hàng đợi, stream lấy trang, worker, khoá, rate limit, phiên bản cài đặt, số đo
    # SLA, chặn đoán mật khẩu, heartbeat health — dữ liệu không được mất (noeviction, có lưu đĩa).
    redis_url: str = "redis://127.0.0.1:16379/0"
    # Redis chỉ cho cache thuần (trang HTTP, robots.txt, tìm nguồn, SERP, câu trả lời LLM, số liệu
    # trang tổng quan): mất / bị đẩy ra thì chỉ tính lại, nên đặt được maxmemory + allkeys-lru.
    # Trống = dùng chung REDIS_URL.
    cache_redis_url: str | None = None
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=10, ge=0)

    # Hàng đợi job (Redis Streams, consumer group): build bộ khung, poll tin mới, index trang tuyển
    # dụng — ba queue riêng để lượt poll (cần nhanh) không phải chờ sau lượt build dài.
    build_queue_name: str = "jobcrawler-build"
    poll_queue_name: str = "jobcrawler-poll"
    board_queue_name: str = "jobcrawler-boards"
    # Job của runner không còn báo sống chừng này giây thì runner khác thu hồi và chạy lại.
    queue_reclaim_seconds: float = Field(default=60, ge=5)
    # Mỗi process runner chạy cùng lúc tối đa chừng này job mỗi loại; 0 = tự tính theo CPU / RAM của
    # máy (`jobcrawler.common.sysres`, như open-asm).
    runner_build_concurrency: int = Field(default=0, ge=0)
    runner_poll_concurrency: int = Field(default=0, ge=0)
    runner_board_concurrency: int = Field(default=0, ge=0)

    # LLM: vLLM self-host (OpenAI-compatible). Để trống thì mọi bước dùng LLM bị tắt.
    llm_base_url: str | None = None
    llm_api_key: SecretStr = SecretStr("EMPTY")
    llm_model: str | None = None
    llm_disable_thinking: bool = True
    llm_max_concurrency: int = Field(default=8, ge=1)
    llm_max_input_chars: int = Field(default=20_000, ge=1_000)
    llm_timeout_seconds: float = Field(default=30, gt=0)
    # Giới hạn request/phút của endpoint, dùng chung mọi worker qua Redis (để trống = không giới
    # hạn). Đặt thấp hơn giới hạn của server một chút; LLM_MAX_CONCURRENCY là mỗi process.
    llm_requests_per_minute: float | None = Field(default=None, gt=0)

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

    # --- Lịch thu thập ---------------------------------------------------------------------------
    # Ban đêm (BUILD_WINDOW) mỗi doanh nghiệp được build lại một lần, rải đều trong khung: KYC (nếu
    # hết hạn), tìm nguồn, đọc đủ danh sách + chi tiết, rồi ghi "bộ khung" (`crawl_plans`: trang
    # danh sách, API, trang danh mục của từng nguồn). Ban ngày (POLL_WINDOW) chỉ poll đúng các
    # trang/API trong bộ khung mỗi POLL_INTERVAL_SECONDS, tải chi tiết tin mới; poll lỗi (trang
    # chính hỏng, cấu trúc đổi, lỗi tạm lặp lại POLL_MAX_FAILURES lần) thì build lại ngay.
    # Khung giờ dạng "HH:MM-HH:MM" theo CRAWL_TIMEZONE; khung qua nửa đêm được ("22:00-05:00").
    crawl_timezone: str = "Asia/Ho_Chi_Minh"
    build_window: str = "00:00-06:00"
    poll_window: str = "06:00-24:00"
    # P95 độ trễ phát hiện tin mới ≈ chu kỳ poll + thời gian một lượt poll (mục tiêu ≤ 3 phút).
    poll_interval_seconds: float = Field(default=150, ge=10)
    # Lệch ngẫu nhiên ± chừng này giây để các doanh nghiệp không dồn vào cùng một giây.
    poll_jitter_seconds: float = Field(default=10, ge=0)
    # Deadline mềm của một lượt poll (huỷ nguồn chưa xong) và timeout cứng của job poll.
    poll_deadline_seconds: float = Field(default=60, gt=0)
    poll_timeout_seconds: int = Field(default=90, gt=0)
    # Lỗi tạm (timeout, 5xx) liên tiếp chừng này lượt poll thì build lại bộ khung.
    poll_max_failures: int = Field(default=3, ge=1)
    # Trang chi tiết tối đa mỗi nguồn trong một lượt poll (tin mới còn lại để lượt sau).
    poll_max_detail_pages: int = Field(default=10, ge=1)
    # Bộ khung mỗi doanh nghiệp: một lượt poll đọc tối đa PLAN_MAX_PAGES trang danh sách / danh
    # mục / API, chia cho các nguồn ở lượt build; mỗi nguồn được ít nhất PLAN_MIN_PAGES trang khi
    # site có đủ. Site nhiều trang hơn bộ khung thì poll chỉ thấy tin mới ở các trang đầu và không
    # đóng việc (lượt build đêm đọc đủ mới đóng).
    plan_min_pages: int = Field(default=3, ge=1)
    plan_max_pages: int = Field(default=10, ge=1)
    # Poll lỗi (bộ khung hỏng) thì build lại ngay, kể cả ban ngày.
    rebuild_on_error: bool = True
    # Build ban đêm tìm lại nguồn trên website (không dùng cache tìm nguồn).
    build_refresh_discovery: bool = True
    # Scheduler: mỗi nhịp xếp tối đa chừng này job; không xếp thêm khi queue còn nhiều việc chờ.
    scheduler_tick_seconds: float = Field(default=1.0, gt=0)
    scheduler_batch: int = Field(default=500, ge=1)
    build_queue_max_backlog: int = Field(default=2000, ge=1)
    poll_queue_max_backlog: int = Field(default=5000, ge=1)

    # --- Gateway gRPC cho worker lấy trang ------------------------------------------------------
    # Worker (gói jobworker, chạy ở máy nào cũng được, kèm proxy riêng) gọi vào gateway; việc lấy
    # trang nằm trong Redis Streams (mỗi pool một stream http + một stream render).
    gateway_host: str = "0.0.0.0"
    gateway_port: int = Field(default=50051, ge=1, le=65535)
    # Token worker phải gửi (metadata `authorization: Bearer <token>`). Trống = không kiểm tra
    # (chỉ dùng trong mạng nội bộ).
    gateway_token: SecretStr | None = None
    # TLS cho gateway (file PEM); trống = không mã hoá (đặt sau reverse proxy có TLS).
    gateway_tls_cert: Path | None = None
    gateway_tls_key: Path | None = None
    # Backend chờ thêm chừng này giây ngoài timeout request cho việc xếp hàng tới worker.
    fetch_queue_grace_seconds: float = Field(default=30, ge=0)
    # Hết khung chờ (timeout + FETCH_QUEUE_GRACE_SECONDS) mà hàng việc vẫn đang được worker lấy
    # (chỉ là đông) thì chờ tiếp từng khung, tổng tối đa timeout + chừng này giây (cũng là hạn
    # `deadline_ms` của việc). Phải lớn hơn FETCH_QUEUE_GRACE_SECONDS.
    fetch_queue_max_wait_seconds: float = Field(default=300, gt=0)
    # Stream việc lấy trang giữ tối đa chừng này entry (việc cũ hơn đã quá hạn).
    fetch_stream_maxlen: int = Field(default=100_000, ge=1000)
    fetch_max_body_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    # Định tuyến theo domain tới pool worker, JSON: FETCH_ROUTES='{"topcv.vn": "residential"}'
    # (khớp cả subdomain). Pool không có worker nào sống thì dùng pool "default".
    fetch_routes: dict[str, str] = Field(default_factory=dict)
    # Worker không báo sống chừng này giây thì coi là đã rời.
    gateway_worker_ttl_seconds: float = Field(default=30, ge=5)
    # Cấu hình đẩy xuống worker (0 / trống = giữ cấu hình của từng worker).
    gateway_http_slots: int = Field(default=0, ge=0)
    gateway_render_slots: int = Field(default=0, ge=0)
    gateway_impersonate: str = "chrome"
    gateway_heartbeat_seconds: int = Field(default=5, ge=1)
    gateway_lease_wait_ms: int = Field(default=1000, ge=50, le=30_000)

    # --- Proxy -----------------------------------------------------------------------------------
    # worker  = mỗi worker dùng proxy của mình (WORKER_PROXIES / WORKER_PROXIES_FILE), không có
    #           thì đi thẳng; backend không chọn proxy.
    # central = backend chọn proxy cho từng request từ danh sách chung (trang Cài đặt, hoặc
    #           PROXIES_FILE), gắn theo domain, nghỉ khi lỗi; worker dùng đúng proxy được chỉ định.
    # off     = không proxy (kể cả proxy tại worker). CLI: --no-proxy.
    proxy_mode: Literal["worker", "central", "off"] = "worker"
    # Danh sách proxy chung khi PROXY_MODE=central (xem config/proxies.example.txt): file và/hoặc
    # danh sách nhập ở trang Cài đặt.
    proxies_file: Path = Path("config/proxies.txt")
    proxies: list[str] = Field(default_factory=list)
    # Tiền tố khoá Redis cho proxy pool, rate limit, robots, HTTP cache, việc lấy trang.
    redis_prefix: str = "jc"

    # HTTP: backend quyết định (robots, rate limit, retry, cache, nhận diện trang chặn); worker
    # chỉ gửi request (curl_cffi impersonate WORKER_IMPERSONATE).
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
    # Trang danh mục / theo tỉnh của trang careers (dẫn tới tin), mỗi nguồn.
    source_max_sub_listings: int = Field(default=60, ge=0)
    # Trang của API danh sách tin mà trang careers SPA gọi (đọc tiếp page / offset), mỗi nguồn.
    source_max_api_pages: int = Field(default=20, ge=1)
    # Tin website/ATS đã tải chi tiết trong chừng này ngày thì dùng lại, không tải lại (lượt sau
    # của doanh nghiệp nhiều tin đi tiếp tới tin chưa tải). 0: luôn tải lại.
    job_reuse_days: float = Field(default=3, ge=0)
    # Request/giây tới API công khai của ATS (host dùng chung cho mọi doanh nghiệp).
    # Ghi đè từng host bằng RATE_LIMITS.
    ats_rate_per_second: float = Field(default=2.0, gt=0)

    # Index các trang tuyển dụng (`jobcrawler index-boards`, cron của `jobcrawler runner`).
    # Danh sách nguồn chạy (rỗng = mọi adapter đang bật), JSON: BOARDS='["topcv","itviec"]'.
    boards: list[str] = Field(default_factory=list)
    # Index đủ (đọc toàn bộ sitemap / danh sách): ban đêm.
    board_index_cron: str = "30 0 * * *"
    # Mỗi lượt index tối đa chừng này giây / trang chi tiết; phần còn lại để lượt sau.
    board_index_budget_seconds: float = Field(default=4 * 3600, gt=0)
    board_max_jobs_per_run: int = Field(default=20_000, ge=1)
    # Index đuôi (ban ngày): chỉ đọc phần mới nhất của danh sách (sitemap mới nhất / trang đầu
    # danh sách mới nhất), tải tin chưa có, rồi poll ngay doanh nghiệp có profile vừa có tin mới.
    board_tail_cron: str = "*/2 6-23 * * *"
    board_tail_budget_seconds: float = Field(default=90, gt=0)
    board_tail_max_jobs: int = Field(default=300, ge=1)
    # Tải lại tin không đổi `lastmod` sau chừng này ngày (kèm ETag/Last-Modified).
    board_refresh_days: float = Field(default=7, gt=0)
    board_concurrency: int = Field(default=4, ge=1)
    # process_company: index của profile cũ hơn chừng này giờ thì crawl trực tiếp trang profile.
    board_stale_hours: float = Field(default=24, gt=0)
    # Map profile ↔ doanh nghiệp: ngưỡng trigram để xét, độ tin cậy tối thiểu để nhận.
    profile_match_trigram: float = Field(default=0.6, gt=0, le=1)
    profile_match_min_confidence: float = Field(default=0.8, ge=0, le=1)
    # KYC không tìm thấy website (chỉ có tên): vẫn dùng profile trên trang tuyển dụng có tên trùng
    # hẳn tên doanh nghiệp (đủ riêng, khớp bằng rule). KYC mơ hồ: chỉ profile khớp MST/website.
    board_name_profiles_without_kyc: bool = True
    # Doanh nghiệp chưa có profile trên một trang: tìm bằng SERP `site:` (cần SERP_API_KEY).
    board_serp_lookup: bool = True
    # Sau mỗi lượt index: doanh nghiệp có profile (đã map) vừa có tin mới được poll ngay.
    board_poll_companies: bool = True
    board_serp_lookup_seconds: float = Field(default=20, gt=0)

    # Chuẩn hoá, xác minh, gộp (process_company sau bước thu thập).
    # Tin còn nghi ngờ phải được LLM chấm ít nhất mức này mới nhận.
    verify_min_confidence: float = Field(default=0.8, ge=0, le=1)
    # JD (mô tả + yêu cầu) ngắn hơn chừng này ký tự thì loại (EMPTY_JD).
    verify_min_jd_chars: int = Field(default=200, ge=0)
    # Tin trên trang tuyển dụng không có hạn nộp, đăng quá chừng này ngày thì coi là hết hạn.
    verify_board_max_age_days: float = Field(default=180, gt=0)
    # Số tin tối đa mỗi doanh nghiệp được hỏi LLM (chấm tin nghi ngờ / phân loại trường mơ hồ).
    verify_llm_max_jobs: int = Field(default=40, ge=0)
    normalize_llm_max_jobs: int = Field(default=40, ge=0)

    # Lượt build: deadline mềm (huỷ các nguồn chưa xong, lưu `partial`) và timeout cứng của job.
    company_deadline_seconds: float = Field(default=165, gt=0)
    company_timeout_seconds: int = Field(default=180, gt=0)
    # Số lần chạy tối đa một job build (runner chết / timeout cứng thì chạy lại).
    company_job_retries: int = Field(default=2, ge=1)
    # Lượt build `partial` (chạm giới hạn, hết giờ, tin chờ LLM): tự xếp lượt chạy tiếp, tối đa
    # chừng này lần liên tiếp, sau chừng này giây (lượt sau dùng lại tin đã tải nên đi tiếp được).
    company_max_continuations: int = Field(default=2, ge=0)
    company_continuation_delay_seconds: float = Field(default=60, ge=0)

    # Bảo trì định kỳ (cron của runner, `jobcrawler db maintain`): dọn dữ liệu cũ, lượt chạy treo,
    # huấn luyện từ điển nén.
    maintenance_cron: str = "17 * * * *"  # mỗi giờ
    overview_cron: str = "*/5 * * * *"  # số liệu phân bố trang tổng quan
    source_fetch_retention_days: float = Field(default=30, gt=0)
    log_retention_days: float = Field(default=30, gt=0)
    failed_page_retention_days: float = Field(default=7, gt=0)
    # Số trang parse lỗi tối đa lưu mỗi lượt (doanh nghiệp / index một trang).
    failed_pages_per_run: int = Field(default=20, ge=0)

    # Nén cột lớn (job_postings.raw, mô tả job, HTML trang lỗi) bằng zstd + từ điển.
    compression_level: int = Field(default=9, ge=1, le=22)
    compression_dict_size: int = Field(default=64 * 1024, ge=4096)
    # Có ít nhất chừng này giá trị mới huấn luyện từ điển; lấy mẫu tối đa `max_samples`.
    compression_dict_min_samples: int = Field(default=1000, ge=10)
    compression_dict_max_samples: int = Field(default=5000, ge=10)
    # Từ điển mới chỉ dùng để nén sau chừng này giây (mọi process đã kịp nạp, xem
    # database/compress).
    compression_dict_delay_seconds: float = Field(default=900, ge=0)
    compression_dict_refresh_seconds: float = Field(default=300, gt=0)
    # Huấn luyện lại khi từ điển đang dùng cũ hơn chừng này ngày.
    compression_dict_retrain_days: float = Field(default=30, gt=0)
    # Số dòng tối đa nén lại bằng từ điển mới mỗi lượt bảo trì.
    compression_recompress_rows: int = Field(default=50_000, ge=0)

    # Tài khoản đăng nhập giao diện / API (trang đăng nhập → JWT, `modules/auth/auth.py`). Không đặt
    # UI_PASSWORD thì không hỏi đăng nhập: chỉ dùng khi cổng chỉ mở ở 127.0.0.1.
    ui_username: str = "admin"
    ui_password: SecretStr | None = None
    # Khoá ký JWT đăng nhập (`modules/auth/auth.py`); trống = khoá ngẫu nhiên lưu trên Redis chính.
    jwt_secret: SecretStr | None = None
    # Khoá mã hoá trường nhạy cảm trong DB (`jobcrawler.services.secretbox`): các chuỗi bí mật cách
    # nhau dấu phẩy, chuỗi cuối dùng để mã hoá. Trống = khoá ngẫu nhiên lưu trên Redis chính.
    encryption_keys: SecretStr | None = None
    # Hạn của phiên đăng nhập (giờ); hết hạn thì đăng nhập lại.
    auth_token_hours: float = Field(default=12, gt=0, le=24 * 30)
    # SSO qua proxy tin cậy (oauth2-proxy đứng trước service giao diện): đặt tên header chứa
    # người dùng đã đăng nhập (vd. X-Auth-Request-User) thì API lấy người dùng từ header đó, không
    # dùng HTTP Basic. CHỈ AN TOÀN khi API không ai gọi thẳng được (chỉ qua giao diện + proxy SSO,
    # proxy xoá / ghi đè header này từ client). Trống = tắt.
    auth_proxy_user_header: str | None = None
    # Header email (vd. X-Auth-Request-Email): dùng khi header người dùng trống.
    auth_proxy_email_header: str | None = None
    # Đường dẫn đăng xuất cho giao diện (vd. /oauth2/sign_out); trống = không có nút đăng xuất.
    auth_logout_url: str | None = None

    # Watchdog trong process api / gateway / scheduler / runner (`jobcrawler.common.watchdog`):
    # event loop hoặc vòng lặp chính đứng quá chừng này giây thì process tự thoát (mã 70) để Docker
    # (`restart: unless-stopped`) dựng lại. Mất Postgres / Redis không tính là đứng. 0 = tắt.
    watchdog_seconds: float = Field(default=120, ge=0)
    # `jobcrawler serve`: bảng `companies` trống thì nạp 50 doanh nghiệp mẫu
    # (data/demo_companies.csv) để thử hệ thống ngay. Production đặt False.
    demo_companies: bool = True

    # Log. Production nên đặt LOG_DB_LEVEL=WARNING (bảng `logs` chỉ giữ cảnh báo / lỗi).
    log_level: LogLevel = "INFO"
    log_format: Literal["console", "json"] = "console"
    log_db_level: LogLevel = "INFO"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Thư mục bí mật đọc lúc tạo Settings (không cố định lúc import): `Settings(_secrets_dir=…)`
        # vẫn thắng; không truyền thì theo SECRETS_DIR / /run/secrets.
        if (
            isinstance(file_secret_settings, SecretsSettingsSource)
            and file_secret_settings.secrets_dir is None
        ):
            file_secret_settings.secrets_dir = secrets_dir()
        # Thứ tự mặc định của pydantic-settings: init > env > .env > file bí mật.
        return init_settings, env_settings, dotenv_settings, file_secret_settings

    @model_validator(mode="after")
    def _check_deadlines(self) -> Self:
        if self.poll_deadline_seconds >= self.poll_timeout_seconds:
            raise ValueError(
                "POLL_DEADLINE_SECONDS phải nhỏ hơn POLL_TIMEOUT_SECONDS "
                f"({self.poll_deadline_seconds} >= {self.poll_timeout_seconds})"
            )
        for name in ("build_window", "poll_window"):
            parse_window(getattr(self, name), name.upper())
        if self.plan_min_pages > self.plan_max_pages:
            raise ValueError(
                "PLAN_MIN_PAGES không được lớn hơn PLAN_MAX_PAGES "
                f"({self.plan_min_pages} > {self.plan_max_pages})"
            )
        if self.gateway_worker_ttl_seconds < 3 * self.gateway_heartbeat_seconds:
            raise ValueError(
                "GATEWAY_WORKER_TTL_SECONDS phải ít nhất gấp 3 GATEWAY_HEARTBEAT_SECONDS "
                f"({self.gateway_worker_ttl_seconds} < 3 × {self.gateway_heartbeat_seconds})"
            )
        if self.fetch_queue_max_wait_seconds <= self.fetch_queue_grace_seconds:
            raise ValueError(
                "FETCH_QUEUE_MAX_WAIT_SECONDS phải lớn hơn FETCH_QUEUE_GRACE_SECONDS "
                f"({self.fetch_queue_max_wait_seconds} <= {self.fetch_queue_grace_seconds})"
            )
        if self.company_deadline_seconds >= self.company_timeout_seconds:
            raise ValueError(
                "COMPANY_DEADLINE_SECONDS phải nhỏ hơn COMPANY_TIMEOUT_SECONDS "
                f"({self.company_deadline_seconds} >= {self.company_timeout_seconds})"
            )
        if self.compression_dict_delay_seconds and (
            self.compression_dict_delay_seconds <= self.compression_dict_refresh_seconds
        ):
            raise ValueError(
                "COMPRESSION_DICT_DELAY_SECONDS phải lớn hơn COMPRESSION_DICT_REFRESH_SECONDS"
            )
        return self

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_base_url and self.llm_model)

    @property
    def serp_enabled(self) -> bool:
        return self.serp_api_key is not None

    @property
    def auth_mode(self) -> AuthMode:
        """Cách API nhận người dùng: `proxy` (header của proxy SSO), `jwt` (đăng nhập bằng
        UI_USERNAME / UI_PASSWORD, phiên là JWT), `none` (không hỏi đăng nhập)."""
        if (self.auth_proxy_user_header or "").strip():
            return "proxy"
        return "jwt" if self.ui_password is not None else "none"

    @property
    def effective_cache_redis_url(self) -> str:
        """URL Redis của cache: CACHE_REDIS_URL, trống thì REDIS_URL."""
        return self.cache_redis_url or self.redis_url

    @property
    def queue_names(self) -> tuple[str, str, str]:
        """(build, poll, boards)."""
        return self.build_queue_name, self.poll_queue_name, self.board_queue_name


@lru_cache
def get_settings() -> Settings:
    return Settings()
