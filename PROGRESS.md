# Tiến độ

Nhật ký theo milestone của `docs/BUILD_PROMPT.md` (mục 12).

## M0 – Khung ✅ (2026-10-16)

### Đã làm

- `pyproject.toml` (uv, Python 3.12, ruff, pyright basic, pytest + pytest-asyncio), `uv.lock`.
- `docker-compose.yaml`: `postgres` (pgvector/pgvector:pg16), `redis` (7-alpine, AOF),
  `migrate` (one-shot `jobcrawler db upgrade`), `worker` (3 replica, cấu hình bằng `WORKER_REPLICAS`).
- `Dockerfile` từ `mcr.microsoft.com/playwright/python:v1.63.0-noble`, chạy bằng `pwuser`.
- `config.py` (pydantic-settings, đủ biến ở mục 11), `.env.example`.
- `logging.py`: structlog → stderr (`ConsoleRenderer` có màu, hoặc JSON với `LOG_FORMAT=json`)
  và → bảng `logs` qua `PgLogSink`: processor đẩy vào `asyncio.Queue`, task nền ghi lô mỗi giây
  bằng `copy_records_to_table`, flush hết khi đóng. Log của thư viện (SAQ, Alembic) đi cùng đường.
- `resources.py`: `open_resources()` mở DB engine, Redis, SAQ queue, `AsyncOpenAI` (nếu đã cấu
  hình), curl_cffi `AsyncSession` (impersonate Chrome), `BrowserManager` (Chromium khởi động lười).
- `schemas.py`: `CompanyIn`, `CompanyEntity`, `JobOut` (đủ trường mục 5), `CompanyResult`, các enum.
- `db/models.py` + migration `0001_initial`: đủ 9 bảng mục 5, extension `pg_trgm`, `unaccent`,
  `vector`; index trigram GIN trên `company_aliases.alias_norm`.
- `text.normalize_company_name()`: tính `alias_norm` (viết thường, bỏ dấu, bỏ loại hình pháp lý).
- `services/companies.process_company()`: tạo/tìm doanh nghiệp, ghi `company_runs` lúc bắt đầu
  (`running`) và kết thúc (kể cả lỗi hoặc bị huỷ), log `company.start` / `company.done`.
  Pipeline bên trong còn rỗng (gắn ở M2–M5).
- CLI typer: `db upgrade|downgrade|current|revision`, `run`, `worker`; các lệnh còn lại có sẵn
  khung, báo "Chưa cài đặt (Mx)" và thoát mã 1.
- Worker SAQ: function `process_company`, startup/shutdown dùng `open_resources`, chỗ cho cron (M4).

### Kiểm tra

- Tiêu chí M0, chạy trên volume trống:
  `docker compose up -d` → `uv run jobcrawler db upgrade` → `uv run jobcrawler run "Test"`
  in `company.start` / `company.done` ra terminal; `company_runs` có 1 dòng `ok`;
  `logs` có `company.start`, `company.done` (kèm `run_id`, `company_id`) và `worker.startup` ×3.
- Đẩy job `process_company` qua SAQ: worker trong container xử lý xong, trả kết quả.
- `docker compose stop worker`: log `worker.shutdown` được flush vào `logs`.
- Trong container: migration được đóng gói đúng, Chromium khởi động được.
- `alembic check`: model khớp migration. Test tích hợp chạy cả `downgrade base` → `upgrade head`.
- `uv run pytest`: 45 test xanh (gồm 2 test tích hợp với Postgres/Redis thật, tự bỏ qua nếu
  không có `TEST_DATABASE_URL`). `ruff check`, `ruff format --check`, `pyright`: sạch.

### Quyết định và chỗ lệch so với tài liệu

- **Cổng host 15432 / 16379** thay vì 5432 / 6379 (máy dev đã có Postgres/Redis khác chiếm cổng);
  đổi được qua `POSTGRES_PORT`, `REDIS_PORT`.
- **Playwright ghim 1.63.0**: PyPI đã có 1.64.0 nhưng MCR chưa có image tương ứng; package và
  image phải cùng phiên bản để dùng browser có sẵn trong image.
- **File compose tên `docker-compose.yaml`** (tài liệu ghi `compose.yaml`; file đã được đổi tên
  trong lúc làm, giữ theo tên hiện tại).
- **Thêm service `migrate`** để worker chỉ khởi động sau khi schema lên head. Nếu không có, lần
  `up -d` đầu tiên trên DB mới làm mất log khởi động của worker (chưa có bảng `logs`).
- **`company_runs.status` có thêm `running`**: dòng được ghi ngay lúc bắt đầu, nên worker chết
  giữa chừng vẫn để lại dấu vết (`running`, `finished_at` rỗng).
- **`companies.kyc_status` cho phép NULL** = chưa chạy KYC.
- **`COMPANY_TIMEOUT_SECONDS=180`** (timeout cứng SAQ) tách khỏi `COMPANY_DEADLINE_SECONDS=165`
  (deadline mềm); validate deadline < timeout.
- Thêm `LOG_LEVEL`, `LOG_FORMAT`, `LOG_DB_LEVEL`, `LLM_TIMEOUT_SECONDS`, `DB_POOL_SIZE`,
  `DB_MAX_OVERFLOW`, `QUEUE_NAME`.
- Schema: `company_aliases.kind` (input/legal/short/brand/english); lương và số năm kinh nghiệm
  của `jobs` lưu thành cột phẳng (`salary_min`, ...), DTO `JobOut` vẫn lồng nhau như mục 5;
  `source_fetches.run_id/company_id` cho phép NULL để dùng chung cho lượt index trang tuyển dụng;
  thêm `created_at`/`updated_at`. Giá trị enum là `TEXT` + `CHECK`, không dùng enum type của PG.
- `LLM_DISABLE_THINKING` mặc định `true`.
- Không thêm thư viện ngoài mục 2. `colorama` (màu terminal trên Windows) đến sẵn qua typer;
  `python-dotenv` đến qua pydantic-settings, chỉ dùng trong test.
- Trên Windows, curl_cffi chạy selector ở thread phụ vì Playwright cần ProactorEventLoop; cảnh
  báo tương ứng được tắt có chủ đích.

### Còn mở / cần người dùng cung cấp

- `LLM_BASE_URL`, `LLM_MODEL` (vLLM + Qwen): cần trước M2 (LLM fallback) và M3 (KYC).
- `SERP_PROVIDER` + `SERP_API_KEY`: cần cho M3.
- `proxies.txt`: cần cho M1 (`proxies check`) và crawl thật.
- Chưa thử `jobcrawler worker` chạy trực tiếp trên Windows (đã chạy trong Docker).
- SAQ huỷ ngay các job đang chạy khi worker dừng (`shutdown_grace_period_s` mặc định 0): xử lý
  ở M6 cùng deadline/partial.
