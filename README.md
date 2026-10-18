# job-crawler

Thu thập tin tuyển dụng đang mở cho ~10.000 doanh nghiệp (chủ yếu tại Việt Nam) từ trang careers,
ATS và các trang tuyển dụng; xác minh, chuẩn hoá, gộp trùng, lưu PostgreSQL.

Đặc tả và milestone: [docs/BUILD_PROMPT.md](docs/BUILD_PROMPT.md). Tiến độ từng milestone ghi
trong commit message.
Vận hành (dịch vụ, giới hạn khi chưa có proxy, quy mô dữ liệu, chống trùng, bảo trì):
[docs/OPERATIONS.md](docs/OPERATIONS.md).

## Chạy nhanh

Cần [uv](https://docs.astral.sh/uv/) và Docker.

```bash
cp .env.example .env                              # điền LLM_*, SERP_* khi có
cp config/proxies.example.txt config/proxies.txt  # điền proxy; không có thì đi thẳng
uv sync
docker compose up -d          # postgres, redis, migrate, worker ×3, indexer, api (giao diện)
uv run jobcrawler db upgrade  # migrate cũng tự chạy trong compose
uv run jobcrawler run "Test"
```

Postgres và Redis mở ra host ở cổng 15432 và 16379 (đổi bằng `POSTGRES_PORT`, `REDIS_PORT`).
Worker đọc proxy từ `./config` (mount read-only vào container).

Không dùng proxy: đặt `PROXY_ENABLED=false` trong `.env` (áp dụng cho cả worker), hoặc thêm
`--no-proxy` cho một lệnh CLI, ví dụ `uv run jobcrawler --no-proxy run "Test"`.

## KYC doanh nghiệp

```bash
uv run jobcrawler kyc "Vinamilk"                          # cần SERP_API_KEY nếu chỉ có tên
uv run jobcrawler kyc "MoMo" --website momo.vn --json
uv run jobcrawler kyc "Công ty CP Sữa Việt Nam" --tax-code 0300588569 --refresh
```

Tra cache `company_aliases` (MST → tên chuẩn hoá khớp chính xác → trigram, chỉ doanh nghiệp đã
`verified`); KYC còn hạn (30 ngày) thì dùng luôn. Không có thì lấy ứng viên từ SERP (website,
trang tra cứu MST) và website (chân trang, JSON-LD: tên pháp lý, MST, địa chỉ), LLM chọn kèm
trích đoạn; rule kiểm lại trích đoạn và tín hiệu khớp mạnh (cùng MST, cùng domain đầu vào, website
tự ghi đúng tên...). Bằng chứng yếu → `ambiguous`, chỉ thu job từ domain khớp mạnh. Kết quả điền
`companies.primary_domain` để `run` tìm nguồn trên website đó. Thiếu `SERP_API_KEY` thì bỏ qua
bước tìm kiếm (cảnh báo `kyc.serp_disabled`).

## Job từ website doanh nghiệp

```bash
uv run jobcrawler site base.vn                 # tìm trang careers/ATS rồi liệt kê job
uv run jobcrawler site momo.vn --discover-only --steps
uv run jobcrawler site https://jobs.lever.co/acme --json
uv run jobcrawler run "Base" --website base.vn # chạy pipeline, ghi company_runs + source_fetches
```

Tìm nguồn: link trên trang chủ → sitemap.xml → đường dẫn phổ biến → subdomain careers./tuyendung.;
nhiều ứng viên thì LLM chọn trong danh sách. Kết quả cache 7 ngày trên Redis (`--refresh` bỏ qua).
ATS có adapter: Greenhouse, Lever, Ashby, Workable, Workday, BambooHR, Recruitee, Teamtailor
(SmartRecruiters chỉ nhận diện: robots.txt cấm API). Trang careers khác: JSON-LD → parser chung →
LLM; mở bằng Chromium khi HTML không có danh sách tin.

## Index trang tuyển dụng

```bash
uv run jobcrawler index-boards --list                      # các trang và trạng thái
uv run jobcrawler index-boards --source itviec             # index tăng dần một trang
uv run jobcrawler index-boards --max-jobs 50 --budget-minutes 10   # chạy thử mọi trang
uv run jobcrawler index-boards --map-only                  # map lại profile ↔ doanh nghiệp
```

Worker chạy cron `BOARD_INDEX_CRON` (mặc định mỗi 5 giờ), mỗi trang một job `index_board`.
Mỗi lượt đọc danh sách tin (sitemap kèm `lastmod`, trang danh sách, API của trang hoặc RSS), chỉ
tải trang chi tiết của tin mới / đã đổi / lâu chưa tải (gửi kèm ETag), lưu `job_postings` và
`source_profiles`, đánh dấu tin vắng 2 lượt là gỡ, rồi map profile có tên gần với doanh nghiệp
đầu vào (MST → website → tên; mơ hồ thì LLM). `process_company` lấy tin từ index của profile đã
map (cũ hơn `BOARD_STALE_HOURS` thì crawl trực tiếp trang profile).
Giới hạn đang đặt khi chưa có proxy và cách nới: [docs/OPERATIONS.md](docs/OPERATIONS.md#index-trang-tuyển-dụng--đang-chạy-không-proxy).

| Trang | Danh sách | Ghi chú |
|---|---|---|
| TopCV | sitemap (~45 nghìn tin) | profile: website trong Organization JSON-LD |
| CareerLink | trang danh sách (~34 nghìn) | hiện CAPTCHA nếu đi nhanh: 1 request / 5 giây |
| JobsGO | sitemap (~28 nghìn) | MST trong mô tả profile |
| CareerViet | sitemap (~19,5 nghìn) | website trong JSON-LD tin, MST trong dữ liệu profile |
| VietnamWorks | API tìm kiếm của trang (~9,8 nghìn) | chi tiết từ payload React |
| Glints | sitemap, không có lastmod (~5,7 nghìn) | |
| TopDev | sitemap (~2,6 nghìn) | |
| Vieclam24h | trang danh sách (chỉ ~1 nghìn tin mới nhất) | website + MST trong dữ liệu trang tin |
| ITviec | sitemap, lastmod vô dụng (~650) | |
| 123job | RSS + trang danh sách | |

## Chuẩn hoá, xác minh, gộp, export

```bash
uv run jobcrawler run "KiotViet" --website kiotviet.vn   # in job đã làm sạch (--json: CompanyResult)
uv run jobcrawler export --format jsonl --out out/        # out/jobs.jsonl, mỗi dòng một JobOut
uv run jobcrawler export --format csv --out out/ --company KiotViet --company 0101234567 [--all]  # tên, MST hoặc id
```

Sau bước thu thập, `process_company` chuẩn hoá từng tin (HTML → markdown sạch, ftfy, bỏ
boilerplate; lương; 34 tỉnh/thành sau sáp nhập 2025, tên cũ → mới; cấp bậc/loại hình/hình thức
bằng rule rồi LLM kèm trích đoạn bằng chứng; ngày về UTC; ẩn SĐT/email cá nhân/tên người liên
hệ), xác minh (`EXPIRED`, `EMPTY_JD`, `EMPLOYER_MISMATCH`, `AGENCY_POSTING`, `SCAM_SIGNALS`,
`NOT_A_JOB`, `SPAM_REPOST`; tin còn nghi ngờ do LLM chấm, nhận khi `confidence ≥
VERIFY_MIN_CONFIDENCE`), gộp trùng (tiêu đề + nơi làm + MinHash JD / cùng link ứng tuyển; bản
chuẩn ATS/careers > trang tuyển dụng JD đầy đủ > mới nhất) rồi upsert `job_postings` (kèm lý do
nhận/loại) và `jobs`. Câu trả lời LLM được cache trên Redis theo nội dung tin. `export` mặc định
chỉ xuất job đang mở; `--all` gồm cả job đã hết hạn / đã gỡ.

## Chạy cả danh sách (10.000 doanh nghiệp)

```bash
uv run jobcrawler import companies.csv           # cột name (bắt buộc), tax_code, website, note
uv run jobcrawler enqueue --all                  # hoặc --file companies.csv, --stale-hours 24
uv run jobcrawler stats [--hours 24] [--json]    # queue, p50/p95, failed, nguồn, trang, nén
uv run jobcrawler eval golden/                   # precision/recall; chưa có golden: 50 job mẫu
uv run jobcrawler db maintain                    # như cron mỗi giờ (dọn dữ liệu cũ, nén lại)
```

- `import` chấp nhận UTF-8 (có BOM), dấu phân cách `,` `;` tab, tiêu đề tiếng Việt ("Tên doanh
  nghiệp", "MST"); MST bị Excel làm mất số 0 đầu được sửa lại; dòng trùng (cùng tên chuẩn hoá +
  MST) bỏ. Chạy lại không tạo trùng (khớp MST rồi tên chuẩn hoá).
- `enqueue`: mỗi doanh nghiệp một job `process_company` (khoá `company:<tên chuẩn hoá>[:MST]`,
  timeout cứng `COMPANY_TIMEOUT_SECONDS`, chạy lại tối đa `COMPANY_JOB_RETRIES` lần khi worker
  chết). Job cùng doanh nghiệp đang chờ/chạy thì bỏ qua; `--stale-hours N` chỉ lấy doanh nghiệp
  chưa có lượt chạy xong (ok/partial/ambiguous) trong N giờ.
- Compose: `worker` (mặc định 3 replica × `WORKER_CONCURRENCY` 10 = 30 doanh nghiệp song song,
  cron bảo trì) và `indexer` (`worker --boards`, queue `BOARD_QUEUE_NAME`: lượt index trang tuyển
  dụng không chiếm chỗ của doanh nghiệp). Ở mốc `COMPANY_DEADLINE_SECONDS` (165 s) các nguồn chưa
  xong bị huỷ, kết quả lưu với trạng thái `partial` kèm `incomplete_sources`.
- `eval golden/`: `golden/companies.csv` + `golden/jobs.csv` (cột `company`, `url` và/hoặc
  `title`); báo cáo ghi ở `out/eval/`. Chưa có golden set: in 50 job ngẫu nhiên và ghi CSV có cột
  `dung` để điền, rồi `jobcrawler eval --reviewed out/eval/sample-....csv` ra precision.

Dữ liệu lớn được nén zstd trước khi lưu: `job_postings.raw`, `jobs.description_md /
requirements_md / benefits_md`, `failed_pages.html_zstd` là bytea (đọc qua ORM như bình thường;
định dạng ở `src/jobcrawler/db/compress.py`). Lượt bảo trì huấn luyện từ điển zstd trên chính dữ
liệu khi đủ mẫu (nén ~4–6 lần thay vì ~1,8 lần của Postgres) rồi nén lại dần dữ liệu cũ.
`source_fetches`, `logs` giữ 30 ngày, `failed_pages` 7 ngày.

## Giao diện web

React + shadcn/ui + Tailwind (thư mục `frontend/`), gọi API JSON của FastAPI (`/api/...`, tài
liệu ở `/api/docs`). Trang: Tổng quan (p95, tỉ lệ failed, biểu đồ lượt chạy, nguồn, nén), Doanh
nghiệp (lọc, nạp CSV, chạy theo lựa chọn / doanh nghiệp cũ / tất cả, chi tiết KYC, profile, lượt
chạy, tin bị loại), Lượt chạy (request, log, trang parse lỗi), Job (lọc theo tỉnh, cấp bậc, lương,
nguồn; tải CSV/JSONL), Kiểm tay (đánh dấu đúng/sai, phím D/S/N, precision theo nguồn), Golden set
(tải companies.csv + jobs.csv → precision/recall), Trang tuyển dụng, Queue.

```bash
docker compose up -d api                 # http://127.0.0.1:8000 (image đã build sẵn giao diện)
```

Chạy trên máy (dev):

```bash
cd frontend && npm install && npm run build   # bản build vào src/jobcrawler/web/dist
uv run jobcrawler ui                           # http://127.0.0.1:8000
cd frontend && npm run dev                     # sửa giao diện: http://localhost:5173 (gọi API cổng 8000)
```

Đặt `UI_PASSWORD` (và `UI_USERNAME`) để hỏi đăng nhập (HTTP Basic); API chặn request ghi đến từ
site khác.

## Kiểm tra hạ tầng

```bash
uv run jobcrawler proxies check     # gọi thử qua từng proxy, cập nhật trạng thái pool
uv run jobcrawler llm check         # gọi thử LLM với structured output
uv run jobcrawler fetch https://example.com/ [--render]   # fetch qua toàn bộ tầng mạng
```

## Phát triển

```bash
uv run ruff format . && uv run ruff check .
uv run pyright
uv run pytest                 # test tích hợp cần TEST_DATABASE_URL (có sẵn trong .env.example)
uv run jobcrawler db revision -m "mô tả"   # sinh migration mới từ model
```

Test không gọi mạng ngoài: nguồn dùng `jobcrawler.fakes.FakeFetcher`, LLM dùng `FakeChatClient`.
