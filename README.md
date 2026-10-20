# job-crawler

Thu thập tin tuyển dụng đang mở cho ~10.000 doanh nghiệp (chủ yếu tại Việt Nam) từ trang careers,
ATS và các trang tuyển dụng; xác minh, chuẩn hoá, gộp trùng, lưu PostgreSQL. Mục tiêu độ tươi: tin
mới của một doanh nghiệp được thấy trong **3 phút ở P95** (ban ngày).

- Kiến trúc (process, luồng dữ liệu, bộ khung build/poll, gateway gRPC, worker):
  [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Triển khai và quy mô 10.000 doanh nghiệp: [docs/DEPLOY.md](docs/DEPLOY.md)
- Vận hành (dịch vụ, giới hạn, bảo trì, sự cố): [docs/OPERATIONS.md](docs/OPERATIONS.md)
- Đặc tả gốc: [docs/BUILD_PROMPT.md](docs/BUILD_PROMPT.md); tiến độ ghi trong commit message.
- Quy ước khi sửa mã (người và coding agent): [AGENTS.md](AGENTS.md); báo lỗ hổng:
  [SECURITY.md](.github/SECURITY.md).

## Cấu trúc repo

Mỗi khối là một service riêng: thư mục của nó có đủ mã, dependency + lockfile, công cụ (lint, kiểu,
test), `Dockerfile`, `Taskfile.yml`, `.env.example` và README; chạy, test, build image độc lập với
khối khác. Gốc repo chỉ gom lại (compose, Taskfile, CI, tài liệu).

| Thư mục     | Service / gói              | Việc                                                                                         |
| ----------- | -------------------------- | -------------------------------------------------------------------------------------------- |
| `backend/`  | image `backend` (`jobcrawler`) | API JSON, gateway gRPC, runner (build / poll / trang tuyển dụng), scheduler, migrate, DB  |
| `worker/`   | image `worker` (`jobworker`)   | lấy trang: nhận việc từ gateway qua gRPC, tải bằng curl_cffi / Chromium qua proxy riêng   |
| `frontend/` | image `web` (nginx)            | giao diện React + shadcn/ui + Tailwind; nginx phục vụ bản build và chuyển `/api` tới `api` |
| `proto/`    | thư viện `jobcrawler-proto`    | hợp đồng gRPC backend ↔ worker (`jcproto/v1/crawl.proto` + mã sinh sẵn), dùng chung        |
| `deploy/`   |                                | compose cho máy backend / máy worker riêng; script khởi tạo Postgres của compose          |
| `docs/`     |                                | kiến trúc, vận hành, triển khai                                                          |

Backend và worker phụ thuộc `proto/` theo đường dẫn (`../proto`); image của chúng nhận `proto/`
làm build context tên `proto`. Frontend sinh client API từ `backend/openapi.json` (backend sở hữu
hợp đồng HTTP của mình).

## Cách hệ thống chạy

- **Ban đêm** (`BUILD_WINDOW`, mặc định 00:00–06:00 giờ Việt Nam) mỗi doanh nghiệp được build một
  lần, rải đều trong khung: KYC, tìm nguồn trên website, đọc đủ danh sách + trang tin, xác minh,
  gộp, lưu, và ghi **bộ khung** của từng nguồn (trang danh sách, API của trang SPA, trang phải mở
  bằng trình duyệt…).
- **Ban ngày** (`POLL_WINDOW`, 06:00–24:00) mỗi doanh nghiệp được poll mỗi 150 giây: chỉ đọc đúng
  các trang / API trong bộ khung, chỉ mở trang của tin mới. Poll lỗi (trang chính hỏng, cấu trúc
  đổi, lỗi lặp lại) thì build lại bộ khung ngay.
- Doanh nghiệp mới nạp được build ngay, không chờ đêm.
- Trang tuyển dụng: index đủ ban đêm, index đuôi (tin mới nhất) mỗi 2 phút ban ngày, rồi poll ngay
  doanh nghiệp có tin mới.
- Mọi request tới website đi qua **worker** (chạy ở máy nào cũng được, kèm proxy riêng); backend
  quyết định robots, rate limit, retry, nhận diện trang chặn.

## Chạy nhanh (một máy)

Cần [uv](https://docs.astral.sh/uv/), Node.js, Docker và [go-task](https://taskfile.dev).

```bash
task setup                           # dependency của mọi service + .env, backend/.env, worker/.env
                                     # (điền LLM_*, SERP_* trong backend/.env khi có)
task up                              # build 3 image, chạy postgres, redis, migrate, api, web, gateway,
                                     # scheduler, runner ×3 vai, worker
task backend:cli -- import companies.csv   # nạp doanh nghiệp: được build ngay rồi poll theo lịch
```

Giao diện: http://127.0.0.1:8000 (service `web`). Postgres / Redis mở ra host ở cổng 15432 / 16379,
gateway gRPC ở `127.0.0.1:50051` (`GATEWAY_PUBLISH`); `api` chỉ nhận từ `web` trong mạng compose.

Chạy các service trên máy (dev) — mỗi lệnh một cửa sổ, cần ít nhất một worker để lấy trang:

```bash
task infra                           # Postgres + Redis
task backend:migrate
task backend:cli -- gateway          # gateway gRPC
task worker:dev                      # worker (worker/.env: GATEWAY_ADDR=127.0.0.1:50051)
task backend:cli -- runner           # cả ba vai build / poll / boards
task backend:cli -- scheduler
task backend:dev                     # API ở cổng 8000
task frontend:dev                    # giao diện http://localhost:5173 (gọi API cổng 8000)
```

## Worker và proxy

```bash
docker compose -f deploy/worker.compose.yaml --env-file deploy/worker.env up -d   # máy worker riêng
task worker:cli -- check             # thử kết nối gateway và từng proxy tại worker
```

- `PROXY_MODE=worker` (mặc định): mỗi worker dùng proxy của mình (`WORKER_PROXIES` /
  `WORKER_PROXIES_FILE`, định dạng như `worker/config/proxies.example.txt`), gắn theo domain, nghỉ
  khi lỗi.
- `PROXY_MODE=central`: backend chọn proxy từ danh sách chung (trang Cài đặt hoặc `PROXIES_FILE`).
- `PROXY_MODE=off` / `jobcrawler --no-proxy …`: không proxy.
- `WORKER_POOL` + `FETCH_ROUTES` (backend): định tuyến domain về nhóm worker riêng (vd. trang tuyển
  dụng qua proxy dân cư).

Chi tiết biến môi trường: [worker/README.md](worker/README.md).

## Lệnh thường dùng

Lệnh `jobcrawler` chạy trong `backend/` (`cd backend && uv run jobcrawler …`, hoặc từ gốc
`task backend:cli -- …`), đọc `backend/.env`.

```bash
uv run jobcrawler import companies.csv           # cột name (bắt buộc), tax_code, website, note
uv run jobcrawler enqueue --all                  # build lại ngay (thường không cần: lịch tự chạy)
uv run jobcrawler build <company_id>             # build một doanh nghiệp trong process này
uv run jobcrawler poll <company_id> [--json]     # poll một doanh nghiệp theo bộ khung
uv run jobcrawler run "KiotViet" --website kiotviet.vn   # chạy đủ pipeline cho một tên (debug)
uv run jobcrawler kyc "Vinamilk" [--refresh]     # chỉ KYC
uv run jobcrawler site base.vn [--discover-only --steps]  # tìm nguồn + liệt kê tin một website
uv run jobcrawler index-boards [--source topcv] [--tail]  # index trang tuyển dụng (đủ / đuôi)
uv run jobcrawler queue info                     # số job chờ / chạy / hẹn giờ của 3 hàng đợi
uv run jobcrawler stats [--hours 24] [--json]
uv run jobcrawler export --format jsonl --out out/
uv run jobcrawler eval golden/
uv run jobcrawler db maintain                    # như cron mỗi giờ
uv run jobcrawler llm check | proxies check | fetch <url> [--render]
```

`import` chấp nhận UTF-8 (có BOM), dấu phân cách `,` `;` tab, tiêu đề tiếng Việt ("Tên doanh
nghiệp", "MST"); MST bị Excel làm mất số 0 đầu được sửa lại; chạy lại không tạo trùng.

## Nguồn

- **ATS**: Greenhouse, Lever, Ashby, Workable, Workday, BambooHR, Recruitee, Teamtailor.
- **Trang careers**: JSON-LD → parser chung → LLM; trang SPA đọc từ JSON nhúng và API trang gọi
  (bộ khung giữ API để poll gọi thẳng, không mở trình duyệt).
- **Trang tuyển dụng**: TopCV, CareerLink, JobsGO, CareerViet, VietnamWorks, Glints, TopDev,
  Vieclam24h, ITviec, 123job — map profile nhà tuyển dụng ↔ doanh nghiệp (MST → website → tên; mơ hồ
  thì LLM).

## Giao diện web

Tổng quan (độ trễ phát hiện tin mới P50/P95, độ tươi, lượt chạy), Hàng đợi, Trang tuyển dụng,
Doanh nghiệp (bộ khung & lịch, Poll ngay / Build lại), Việc làm, Kiểm tay, Golden set, **Workers**
(worker đang kết nối, slot, proxy), **Cài đặt** (lịch, runner, worker, proxy, HTTP, nguồn, trang
tuyển dụng, LLM/SERP, xác minh, bảo trì — đổi ở giao diện có hiệu lực trong vài giây, mục nào cần
khởi động lại được ghi rõ), **Nhật ký thao tác**. Đặt `UI_PASSWORD` trong `backend/.env` để hỏi
đăng nhập. Chi tiết build, nginx, client sinh từ OpenAPI: [frontend/README.md](frontend/README.md).

## Phát triển

Mỗi service có `Taskfile.yml` riêng; `Taskfile.yml` ở gốc gom lại theo tên service
(`task --list` để xem hết). Trong thư mục một service, `task <lệnh>` chạy riêng service đó.

```bash
task check                            # mọi thứ CI chạy, từng service (proto, backend, worker, frontend)
task backend:check                    # chỉ backend: ruff, pyright, OpenAPI, pytest
task backend:test:unit                # pytest backend bỏ test tích hợp
task proto:gen                        # sau khi sửa proto/jcproto/v1/crawl.proto
task api                              # sau khi đổi mô hình trả về của API: backend/openapi.json + client frontend
task backend:migration NAME="mô tả"   # sinh migration mới từ model
```

- Test không gọi mạng ngoài: nguồn dùng `jobcrawler.fakes.FakeFetcher`, LLM dùng
  `FakeChatClient`, worker dùng gateway giả + server HTTP cục bộ. Test tích hợp cần
  `TEST_DATABASE_URL` trỏ tới DB tên kết thúc bằng `_test` (schema bị xoá và dựng lại).
- CI (`.github/workflows/ci.yml`) chạy kiểm tra của từng service (một job mỗi service) mỗi lần
  push lên `main`, kèm build 3 image; `audit.yml` kiểm lỗ hổng dependency mỗi tuần.
- Hook tuỳ chọn: `uvx pre-commit install -t pre-commit -t commit-msg` (ruff, gitleaks, kiểm tra
  commit message kiểu Conventional Commits).
