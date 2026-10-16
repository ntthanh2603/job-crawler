# Prompt: Xây dựng hệ thống thu thập job cho 10.000 doanh nghiệp

> Cách dùng: đưa toàn bộ file này cho AI coding agent và yêu cầu nó làm **lần lượt từng milestone ở mục 12**, không làm tất cả trong một lượt. Ví dụ: "Đọc docs/BUILD_PROMPT.md và làm M0."

---

## 1. Vai trò và mục tiêu

Bạn là kỹ sư backend/data senior. Hãy xây dựng trong repo này một hệ thống Python:

- **Input:** danh sách khoảng 10.000 doanh nghiệp (chủ yếu tại Việt Nam), mỗi doanh nghiệp tối thiểu có tên.
- **Output:** toàn bộ tin tuyển dụng đang mở của từng doanh nghiệp, thu từ nhiều nguồn (trang careers của chính doanh nghiệp, ATS, các trang tuyển dụng). Mỗi tin phải được xác minh đúng doanh nghiệp, làm sạch, chuẩn hoá, gộp trùng, lưu vào PostgreSQL và export được ra JSONL/CSV.
- **Ưu tiên độ chính xác hơn số lượng:** thà bỏ sót một tin mơ hồ còn hơn trả về job của công ty khác, job hết hạn hay tin rác.

Bạn được tự quyết định crawl những trang tuyển dụng và website doanh nghiệp nào, miễn tuân thủ mục 9 (quy tắc crawl) và đạt tiêu chí ở mục 13.

## 2. Ràng buộc kỹ thuật (bắt buộc)

| Hạng mục | Lựa chọn |
|---|---|
| Ngôn ngữ | Python 3.12, asyncio toàn bộ |
| Công cụ | uv (package), ruff (lint + format), pyright (basic), pytest + pytest-asyncio |
| Queue + cron | SAQ trên Redis |
| DB | PostgreSQL 16 với `pg_trgm`, `unaccent` (cài sẵn `pgvector` cho sau này); SQLAlchemy 2.0 async + asyncpg; Alembic |
| HTTP | curl_cffi (impersonate Chrome) |
| Trình duyệt | Playwright (Chromium), chỉ dùng khi HTTP không đủ |
| Parse | selectolax, extruct (JSON-LD), markdownify hoặc trafilatura (HTML → markdown) |
| Làm sạch / gộp | rapidfuzz, datasketch (MinHash), ftfy |
| LLM | vLLM self-host chạy Qwen, gọi qua SDK `openai` (`AsyncOpenAI(base_url=...)`) |
| Validation / config | Pydantic v2, pydantic-settings |
| Log | structlog |
| CLI | typer |
| Triển khai | Docker Compose: `postgres`, `redis`, `worker` (nhiều replica) |

**Không dùng:** Temporal, Celery, S3/MinIO, Kubernetes, Elasticsearch, LangChain/LangGraph hay agent framework nào, SDK của nhà cung cấp LLM khác. Chưa làm FastAPI ở giai đoạn này.

## 3. Yêu cầu phi chức năng

- 10.000 doanh nghiệp mỗi lượt, chạy lại được nhiều lượt (refresh định kỳ).
- 30 doanh nghiệp xử lý đồng thời: mặc định 3 worker × concurrency 10, cấu hình được.
- Mỗi doanh nghiệp tối đa 180 giây. Ở mốc 165 giây phải huỷ các nguồn chưa xong, lưu kết quả đã có với trạng thái `partial` và ghi rõ nguồn nào chưa xong.
- Khoảng 100 proxy (HTTP/SOCKS) đọc từ file, xoay vòng.
- Idempotent: chạy lại một doanh nghiệp không sinh bản ghi trùng.
- Worker chết giữa chừng thì SAQ chạy lại job; các bước đã cache (KYC, tìm nguồn) không phải làm lại.

## 4. Kiến trúc

```
CLI (typer) ──enqueue──▶ Redis (SAQ queue, rate limit, proxy pool)
Cron (SAQ)  ───────────▶        │
                                ▼
            Worker asyncio × N (mỗi worker 10 doanh nghiệp)
            KYC → tìm nguồn → thu thập song song → chuẩn hoá → xác minh → gộp → lưu
                                │
      PostgreSQL (dữ liệu + log) · vLLM Qwen · job sites (qua proxy) · SERP API
```

Hai luồng thu thập:

1. **Index trước (cron):** crawl tăng dần toàn bộ tin đang mở trên mỗi trang tuyển dụng lớn vào bảng `job_postings`, kèm profile nhà tuyển dụng trên trang đó. Map profile với doanh nghiệp một lần rồi cache.
2. **Theo doanh nghiệp (queue):** mỗi doanh nghiệp là một job SAQ `process_company`. Job này chạy KYC, tìm nguồn, lấy tin từ index, crawl trực tiếp trang careers/ATS, rồi chuẩn hoá, xác minh, gộp và lưu.

### Cấu trúc thư mục

Giữ đúng cấu trúc này để sau này gắn FastAPI mà không phải sửa lõi.

```
src/jobcrawler/
  config.py           Settings (pydantic-settings), đọc .env
  resources.py        open_resources(): async context manager mở/đóng DB engine,
                      Redis, SAQ queue, AsyncOpenAI, HTTP client, browser
  schemas.py          Pydantic DTO: CompanyIn, CompanyEntity, JobOut, CompanyResult, ...
  logging.py          structlog → terminal + Postgres
  llm.py              client vLLM + hàm gọi structured output
  prompts/            prompt cho LLM, mỗi prompt một file, có version
  services/           logic gọi được từ mọi entrypoint: lookup.py, companies.py, jobs.py, export.py
  pipeline/           kyc.py, discovery.py, collect.py, normalize.py, verify.py, dedup.py
  sources/
    base.py           giao diện adapter
    boards/           mỗi trang tuyển dụng một file
    ats/              greenhouse.py, lever.py, ashby.py, smartrecruiters.py, workable.py, workday.py, ...
    careers.py        trang careers chung: JSON-LD → parser → LLM fallback
  net/                http.py, browser.py, proxy_pool.py, ratelimit.py, robots.py
  db/                 models.py, session.py, alembic/
  entrypoints/
    cli.py
    worker.py         cấu hình SAQ (functions, cron_jobs, startup/shutdown dùng open_resources)
    api/              để trống, FastAPI sẽ thêm sau
tests/
  fixtures/<source>/  HTML/JSON thật đã lưu
docs/
  sources.md          bảng nguồn: phương thức, rate limit, trạng thái, ghi chú
PROGRESS.md           nhật ký tiến độ theo milestone
compose.yaml, Dockerfile, .env.example
```

Quy tắc:

- Entrypoint (CLI, worker, sau này là API) chỉ gọi vào `services/`. Không đặt logic nghiệp vụ trong entrypoint.
- Mọi I/O đều async.
- Schema Pydantic tách riêng khỏi model SQLAlchemy.

## 5. Dữ liệu

### Input

File `companies.csv` gồm các cột: `name` (bắt buộc), `tax_code`, `website`, `note` (tuỳ chọn). Nhiều dòng có thể chỉ có tên.

### Bảng (tạo bằng Alembic)

- **`companies`:** id, input_name, legal_name, brand_name, tax_code (unique, nullable), primary_domain, other_domains[], address, kyc_status (`verified` | `ambiguous` | `not_found`), kyc_confidence, kyc_evidence jsonb (URL + trích đoạn làm bằng chứng), kyc_at.
- **`company_aliases`:** company_id, alias, alias_norm, có index trigram. `alias_norm` là tên đã viết thường, bỏ dấu và bỏ hậu tố pháp lý ("công ty TNHH", "cổ phần", "JSC", "Co., Ltd"...).
- **`source_profiles`:** source, external_id, url, name_on_source, company_id (nullable), match_confidence, match_evidence jsonb, unique(source, external_id).
- **`job_postings`:** mỗi tin trên mỗi nguồn: source, external_id, url, source_profile_id, job_id (nullable), raw jsonb (các trường đã bóc), content_hash, first_seen_at, last_seen_at, status, unique(source, external_id).
- **`jobs`:** job sau khi gộp, gồm các trường của `JobOut` bên dưới, cộng `canonical_posting_id` và `cluster_key`.
- **`company_runs`:** run_id, company_id, started_at, finished_at, duration_ms, status (`ok` | `partial` | `failed` | `ambiguous`), jobs_found, jobs_accepted, incomplete_sources[], error.
- **`source_fetches`:** run_id, company_id, source, url, http_status, proxy_id, duration_ms, items, outcome (`ok` | `blocked` | `timeout` | `error` | `robots_disallowed`), error.
- **`logs`:** ts, level, event, run_id, company_id, context jsonb.
- **`failed_pages`:** url, source, reason, html_zstd bytea, created_at. Chỉ lưu khi parse lỗi, xoá sau 7 ngày.

### Output: `JobOut` (Pydantic)

- `title`, `title_normalized`
- `company_id`, `company_name`
- `level`: intern | fresher | junior | middle | senior | lead | manager | director | unknown
- `employment_type`: full_time | part_time | contract | internship | freelance | unknown
- `work_mode`: onsite | hybrid | remote | unknown
- `locations`: list[{province, address_raw}]. Tỉnh/thành chuẩn hoá theo danh mục 34 đơn vị sau sáp nhập năm 2025, có bảng ánh xạ tên cũ sang tên mới (từ 7/2025 không còn cấp huyện).
- `salary`: {min, max, currency, period (month | year | hour), negotiable: bool, raw}
- `experience_years`: {min, max}
- `description_md`, `requirements_md`, `benefits_md`: markdown sạch (bỏ HTML, emoji trang trí, khoảng trắng thừa; sửa lỗi encoding)
- `skills`: list[str]
- `posted_at`, `expires_at`
- `apply_url`, `source_urls`: list[str], `canonical_source`
- `status`: active | expired | removed
- `confidence`: 0..1, `verification_reasons`: list[str]
- `first_seen_at`, `last_seen_at`

Quy tắc dữ liệu cá nhân: không lưu số điện thoại, email cá nhân hay tên người liên hệ xuất hiện trong JD; thay bằng `[liên hệ đã ẩn]`. Được giữ email ứng tuyển chung của công ty và URL ứng tuyển.

## 6. Pipeline `process_company`

Thời gian: deadline mềm 165 giây (đo bằng `time.monotonic()`), timeout SAQ 180 giây.

Khi enqueue:

- SAQ có timeout mặc định rất ngắn, nên luôn đặt `timeout=180`.
- Job key là `company:{alias_norm}` để không nạp trùng một doanh nghiệp.

### 6.1 KYC doanh nghiệp (`pipeline/kyc.py`)

- **Tra cache** trong `company_aliases`: khớp chính xác `alias_norm` trước, rồi khớp trigram vượt ngưỡng. Nếu tìm thấy và `kyc_at` chưa quá 30 ngày thì dùng luôn.
- **Nếu không có trong cache**, thu thập ứng viên từ:
  - SERP: website chính thức, các trang tra cứu thông tin doanh nghiệp / MST công khai;
  - website doanh nghiệp: footer thường có tên pháp lý, MST, địa chỉ.
- **Chọn ứng viên:** đưa danh sách ứng viên cho LLM chọn. LLM trả JSON kèm bằng chứng (URL + trích đoạn).
- **Bằng chứng yếu:**
  - đánh `ambiguous` và lưu danh sách ứng viên;
  - chỉ lấy job từ nguồn có bằng chứng khớp mạnh (cùng domain hoặc cùng MST), bỏ qua phần còn lại.
- **Lưu alias:** tên input, tên pháp lý, tên viết tắt, brand, tên tiếng Anh.

### 6.2 Tìm nguồn (`pipeline/discovery.py`, cache 7 ngày)

- **Website:** lấy từ kết quả KYC.
- **Trang careers:**
  - thử các đường dẫn phổ biến: `/careers`, `/career`, `/jobs`, `/tuyen-dung`, `/co-hoi-nghe-nghiep`, `/en/careers`...;
  - xem link trên trang chủ có chứa từ khoá: tuyển dụng, careers, jobs, join us, cơ hội nghề nghiệp;
  - đọc `sitemap.xml`.
  - Tối đa 3 bước nhảy. Khi có nhiều link ứng viên, đưa danh sách cho LLM chọn: LLM chỉ được chọn trong danh sách và trả JSON. **Không** làm vòng lặp agent tự do.
- **ATS:** nhận diện theo URL hoặc HTML:
  - `boards.greenhouse.io`, `job-boards.greenhouse.io`;
  - `jobs.lever.co`, `jobs.ashbyhq.com`, `apply.workable.com`;
  - `*.myworkdayjobs.com`, `careers.smartrecruiters.com`;
  - `*.bamboohr.com`, `*.recruitee.com`, `*.teamtailor.com`;
  - các ATS phổ biến tại Việt Nam mà bạn khảo sát được.
- **Profile trên các trang tuyển dụng:** lấy từ `source_profiles` đã map khi index. Nếu chưa có, tìm trên chính trang đó (dùng ô tìm kiếm của trang nếu có, hoặc SERP với `site:`).

### 6.3 Thu thập (`pipeline/collect.py`)

- Chạy song song mọi nguồn bằng `asyncio.wait(..., timeout=<thời gian còn lại tới deadline>)`. Huỷ nguồn quá hạn và ghi vào `incomplete_sources`.
- Thứ tự ưu tiên khi bóc dữ liệu:
  1. API JSON công khai của ATS;
  2. JSON-LD `schema.org/JobPosting`;
  3. parser riêng của từng site;
  4. LLM bóc từ markdown của trang (fallback, theo schema).
- Tin trên trang tuyển dụng lấy từ bảng `job_postings` đã index. Chỉ crawl trực tiếp khi index của profile đó cũ hơn 24 giờ.

### 6.4 Chuẩn hoá (`pipeline/normalize.py`)

- **Văn bản:** chuyển HTML sang markdown sạch, chạy ftfy, bỏ phần boilerplate (menu, footer, nút chia sẻ...).
- **Lương:**
  - parse các dạng "10 - 15 triệu", "Tới 30 triệu", "Từ 1,000 USD", "$1.5k-2k", "Thỏa thuận", "Cạnh tranh";
  - xác định tiền tệ và kỳ trả.
- **Địa điểm:**
  - chuẩn hoá về danh mục 34 tỉnh/thành, ánh xạ tên cũ sang tên mới;
  - "Hà Nội", "HN", "Ha Noi" phải ra cùng một giá trị.
- **Cấp bậc, loại hình, hình thức làm việc:** dùng rule trước, chỉ hỏi LLM khi không rõ.
- **Ngày:**
  - chuẩn hoá về UTC;
  - xử lý dạng tương đối ("2 ngày trước") và dạng "Hạn nộp: 30/11/2025".

### 6.5 Xác minh job (`pipeline/verify.py`)

Rule loại trước, mỗi rule có một mã lý do:

- **`EXPIRED`:** đã hết hạn, hoặc không còn thấy trên nguồn qua 2 chu kỳ index.
- **`EMPTY_JD`:** mô tả sau khi làm sạch ngắn hơn 200 ký tự.
- **`EMPLOYER_MISMATCH`:** nhà tuyển dụng trên tin không khớp doanh nghiệp, ví dụ profile chưa map, tên khác hẳn, hoặc link ứng tuyển trỏ về domain lạ.
- **`AGENCY_POSTING`:** headhunter hoặc agency đăng hộ ("tuyển cho đối tác", "khách hàng của chúng tôi", "công ty bảo mật", "confidential").
- **`SCAM_SIGNALS`:** thu phí, đặt cọc, đa cấp, "việc nhẹ lương cao", tuyển cộng tác viên online.
- **`NOT_A_JOB`:** trang tin tức, blog hoặc trang danh mục.
- **`SPAM_REPOST`:** cùng một nội dung bị đăng lại hàng loạt.

Tin qua được rule nhưng vẫn còn nghi ngờ thì nhờ LLM chấm, trả về `{is_job_posting, belongs_to_company, is_agency, scam_risk, confidence, reasons}`.

- Ngưỡng nhận mặc định: `confidence ≥ 0.8` (cấu hình được).
- Lưu lý do cho mọi quyết định nhận hoặc loại.

### 6.6 Gộp trùng (`pipeline/dedup.py`)

- Chỉ so các tin trong cùng một doanh nghiệp.
- Hai tin thuộc cùng một cụm khi thoả **cả ba** điều kiện:
  1. `title_normalized` giống nhau (rapidfuzz `token_set_ratio ≥ 90`);
  2. có chung ít nhất một địa điểm;
  3. JD giống nhau (MinHash Jaccard ≥ 0.8) **hoặc** cùng `apply_url`.
- Chọn bản chuẩn theo thứ tự ưu tiên:
  1. ATS hoặc trang careers của doanh nghiệp;
  2. trang tuyển dụng có JD đầy đủ nhất;
  3. tin mới nhất.
- Giữ lại mọi `source_urls` của cụm.

### 6.7 Lưu

- Upsert theo khoá duy nhất.
- Ghi `company_runs` và `source_fetches` cho mọi lần chạy, kể cả khi lỗi.

## 7. Index các trang tuyển dụng (cron SAQ)

- **Adapter và lịch chạy:** mỗi trang tuyển dụng một adapter trong `sources/boards/`; cron mỗi 4–6 giờ (cấu hình được).
- **Crawl tăng dần:** đọc sitemap hoặc trang listing, chỉ fetch URL mới hoặc đã thay đổi (dựa vào ETag, Last-Modified hoặc `content_hash`).
- **Lưu và map:**
  - lưu tin vào `job_postings` và profile vào `source_profiles`;
  - chỉ chạy KYC/map cho profile có tên gần với một doanh nghiệp đầu vào (so trigram), để tiết kiệm LLM.
- **Nguồn khởi điểm:** TopCV, VietnamWorks, CareerViet, ITviec, TopDev, JobsGO, CareerLink, Glints Việt Nam, Vieclam24h, 123job. Tự khảo sát để thêm hoặc bớt, ưu tiên theo độ phủ, và ghi vào `docs/sources.md`.
- **Không dùng** LinkedIn, Facebook hay nguồn nào bắt đăng nhập.

## 8. LLM (vLLM + Qwen)

- **Client:** `AsyncOpenAI(base_url=LLM_BASE_URL, api_key=LLM_API_KEY)`, model lấy từ `LLM_MODEL`.
- **Structured output:**
  - mọi lời gọi trả về JSON theo một schema Pydantic: `response_format={"type": "json_schema", "json_schema": {"name": ..., "schema": Model.model_json_schema()}}`;
  - kiểm tra lại bằng `Model.model_validate_json(...)`;
  - lỗi parse thì retry một lần, vẫn lỗi thì coi kết quả là `unknown`.
- **Tham số:**
  - `temperature=0`;
  - nếu model Qwen có chế độ thinking, cho phép tắt qua `extra_body={"chat_template_kwargs": {"enable_thinking": False}}`, bật/tắt bằng config `LLM_DISABLE_THINKING`.
- **Giới hạn:**
  - đồng thời bằng semaphore (`LLM_MAX_CONCURRENCY`, mặc định 8 mỗi process);
  - timeout 30 giây mỗi lời gọi.
- **Input:** chỉ gửi markdown đã làm sạch, cắt tối đa `LLM_MAX_INPUT_CHARS` ký tự.
- **Prompt và log:** prompt đặt trong `src/jobcrawler/prompts/`; log thời gian và số token của mỗi lời gọi.
- **Chỉ dùng LLM cho:**
  - chọn ứng viên KYC;
  - chọn link careers trong một danh sách cho sẵn;
  - bóc job khi không có dữ liệu cấu trúc;
  - phân loại các trường mơ hồ;
  - chấm các job còn nghi ngờ.

  Việc gì làm được bằng rule thì làm bằng rule.

## 9. Quy tắc crawl

- **Proxy:**
  - đọc `proxies.txt`, mỗi dòng một URL proxy;
  - pool trên Redis: gắn proxy theo domain (sticky session), chấm điểm sức khoẻ, cho nghỉ khi gặp 403/429, loại khi lỗi liên tiếp;
  - có lệnh `jobcrawler proxies check`.
- **Rate limit:**
  - theo domain, dùng token bucket trên Redis để mọi process dùng chung;
  - mặc định 1 request/giây mỗi domain, cấu hình riêng được cho từng nguồn.
- **robots.txt:** tôn trọng `robots.txt` (cache 24 giờ) và `Crawl-delay`.
- **Retry:** backoff + jitter cho 429, 5xx và timeout, tối đa 3 lần.
- **Bị chặn:**
  - gặp CAPTCHA, trang bắt đăng nhập hoặc trang chặn bot: dừng nguồn đó trong lần chạy hiện tại và ghi `blocked`;
  - **không** tìm cách vượt: không giải CAPTCHA, không đăng nhập.
- **User-Agent:** nhất quán với fingerprint đang impersonate.
- **Playwright:**
  - một browser mỗi process, mỗi job một context;
  - tối đa `BROWSER_MAX_PAGES` trang đồng thời (mặc định 4);
  - chặn tải ảnh, font, media.
- **Cache:** HTTP cache ngắn hạn trong Redis (theo URL, TTL 1 giờ) để không fetch lại cùng một URL trong một lượt.

## 10. Log

- **Context:** structlog, gắn `run_id`, `company`, `source` qua contextvars.
- **Terminal:** `ConsoleRenderer` có màu. Mỗi doanh nghiệp xong in một dòng tóm tắt: thời gian chạy, số job tìm thấy / được nhận, các nguồn chưa xong.
- **Postgres:**
  - một processor đẩy event vào `asyncio.Queue`;
  - task nền ghi theo lô mỗi giây bằng `copy_records_to_table` của asyncpg;
  - không được chặn worker; khi shutdown phải flush hết.
- **Tên event ví dụ:** `company.start`, `kyc.done`, `discovery.done`, `source.fetch`, `source.blocked`, `verify.reject`, `company.done`.

## 11. CLI và cấu hình

```
jobcrawler db upgrade
jobcrawler import companies.csv
jobcrawler enqueue [--all | --file companies.csv | --stale-hours 24]
jobcrawler run "<tên doanh nghiệp>" [--json]
jobcrawler worker
jobcrawler index-boards [--source topcv]
jobcrawler export --format jsonl|csv --out out/ [--company ...]
jobcrawler stats
jobcrawler eval golden/
jobcrawler proxies check
```

Cấu hình qua `.env` (tạo sẵn `.env.example`):

- **Kết nối:** `DATABASE_URL`, `REDIS_URL`.
- **LLM:** `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, `LLM_DISABLE_THINKING`, `LLM_MAX_CONCURRENCY`, `LLM_MAX_INPUT_CHARS`.
- **SERP:** `SERP_PROVIDER` (serper | brave), `SERP_API_KEY`.
- **Crawl và worker:** `PROXIES_FILE`, `WORKER_CONCURRENCY`, `BROWSER_MAX_PAGES`, `COMPANY_DEADLINE_SECONDS`.

Nếu thiếu `SERP_API_KEY` thì bỏ qua các bước dùng SERP và ghi cảnh báo, không được crash.

## 12. Milestones

Làm tuần tự. Mỗi milestone chỉ xong khi: test xanh, ruff sạch, đã commit, và đã cập nhật `PROGRESS.md`.

- **M0 – Khung**
  - Làm: `pyproject.toml`; `compose.yaml` (Postgres có pgvector + Redis); `Dockerfile` dựng từ image Playwright Python; `config`, `resources`, `logging`; migration Alembic đầu tiên; CLI và worker rỗng.
  - Xong khi: `docker compose up -d` → `jobcrawler db upgrade` → `jobcrawler run "Test"` in log ra terminal và ghi dòng vào `company_runs` và `logs`.
- **M1 – Tầng mạng + LLM**
  - Làm: HTTP client (curl_cffi + proxy pool + rate limit + robots + retry), browser, client LLM có structured output.
  - Xong khi: test đơn vị chạy qua fake fetcher; `proxies check` chạy được.
- **M2 – Nguồn từ website doanh nghiệp**
  - Làm: tìm trang careers, adapter ATS, extractor JSON-LD, LLM fallback.
  - Xong khi: đưa vào một domain thì ra danh sách job; có test bằng fixture.
- **M3 – KYC doanh nghiệp**
  - Xong khi: có test, và đã kiểm tay 20 doanh nghiệp mẫu, ghi kết quả vào `PROGRESS.md`.
- **M4 – Index trang tuyển dụng**
  - Làm: adapter cho từng trang (theo thứ tự độ phủ), cron, map profile.
  - Xong khi: mỗi adapter có test fixture; `index-boards` chạy tăng dần và ghi số lượng.
- **M5 – Chuẩn hoá, xác minh, gộp, export**
  - Xong khi: `run "<tên>"` in ra job đã làm sạch; `export` ra JSONL đúng schema `JobOut`.
- **M6 – Quy mô**
  - Làm: enqueue 10.000 doanh nghiệp, deadline + kết quả một phần, `stats`, `eval`, compose nhiều replica.
  - Xong khi: chạy liên tục 200 doanh nghiệp không lỗi, p95 ≤ 180 giây, có báo cáo `eval`.

## 13. Tiêu chí chất lượng

- **Golden set:** người dùng cung cấp `golden/companies.csv` và `golden/jobs.csv` (URL hoặc tiêu đề các job đúng). `jobcrawler eval` tính precision và recall theo từng doanh nghiệp và từng nguồn.
- **Mục tiêu mặc định:**
  - precision ≥ 95%;
  - recall ≥ 85%;
  - p95 thời gian mỗi doanh nghiệp ≤ 180 giây;
  - tỉ lệ `failed` < 2%.
- **Chưa có golden set:** in báo cáo gồm 50 job chọn ngẫu nhiên để người kiểm tay.

## 14. Cách làm việc

- **Trước khi code:** đọc hết tài liệu này, rồi lập kế hoạch cho milestone hiện tại.
- **Không bịa cấu trúc site.** Trước khi viết adapter cho một site:
  1. mở trang thật: `robots.txt`, sitemap, JSON-LD, các request JSON mà frontend gọi;
  2. lưu 2–3 mẫu vào `tests/fixtures/<source>/`;
  3. viết test parser trên các mẫu đó;
  4. rồi mới viết adapter.
- **Test không gọi mạng:** tầng fetch có interface để inject fake.
- **Ghi nguồn:** mọi nguồn đều có một dòng trong `docs/sources.md` (nguồn, phương thức, rate limit, trạng thái, ghi chú).
- **Thư viện:** không thêm ngoài mục 2 nếu không thật cần; nếu thêm, ghi lý do vào `PROGRESS.md`.
- **Hỏi khi thiếu:** thông tin chỉ người dùng có (URL vLLM, tên model, file proxy, SERP API key, golden set) thì hỏi, không tự bịa giá trị.
- **Ngoài phạm vi:** API FastAPI, giao diện, xác thực người dùng, S3, Temporal, Kubernetes.
