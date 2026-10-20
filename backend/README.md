# jobcrawler (backend)

Service backend của jobcrawler: gói Python `jobcrawler`, một image Docker chạy nhiều vai (khác lệnh
chạy). Không có Chromium / curl_cffi: mọi request tới website đi qua **worker** lấy trang (`../worker`)
qua gateway gRPC. Giao diện React là service riêng (`../frontend`, nginx) gọi API này qua `/api`.

| Lệnh                                  | Vai                                                                    |
| ------------------------------------- | ---------------------------------------------------------------------- |
| `jobcrawler api`                      | API JSON (cổng 8000): `/api/...`, `/api/docs`, `/healthz`              |
| `jobcrawler gateway`                  | gateway gRPC cho worker (cổng 50051); healthcheck `gateway-health`     |
| `jobcrawler scheduler`                | xếp lịch build bộ khung ban đêm, poll tin mới ban ngày                 |
| `jobcrawler runner --role build`      | job trong hàng đợi Redis Streams; vai `build`, `poll`, `boards`        |
| `jobcrawler db upgrade`               | migrate schema Postgres (Alembic), chạy một lần trước các vai khác     |

Ngoài ra CLI có lệnh vận hành / debug: `import`, `enqueue`, `build`, `poll`, `run`, `kyc`, `site`,
`index-boards`, `queue`, `stats`, `export`, `eval`, `db maintain`, `llm check`, `proxies check`,
`fetch` (`uv run jobcrawler --help`). `jobcrawler ui` là tên cũ của `jobcrawler api` (ẩn, sẽ bỏ).

Cần: Postgres 16 (pgvector/pg_trgm), Redis 7, ít nhất một worker nối vào gateway. Hợp đồng gRPC
dùng chung với worker nằm ở `../proto` (gói `jobcrawler-proto`, phụ thuộc qua đường dẫn, cài dạng
editable khi dev).

## Chạy trên máy

Mọi lệnh chạy trong thư mục `backend/` (venv `.venv`, `uv.lock`, `.env` đều ở đây). Cần
[uv](https://docs.astral.sh/uv/); [Task](https://taskfile.dev) không bắt buộc (`task --list`).

```bash
cd backend
uv sync                              # tạo .venv, cài dependency + công cụ dev theo uv.lock
cp .env.example .env                 # điền LLM_*, SERP_* khi có
uv run jobcrawler db upgrade
uv run jobcrawler gateway            # cửa sổ 1 (worker nối vào 127.0.0.1:50051)
uv run jobcrawler runner             # cửa sổ 2
uv run jobcrawler scheduler          # cửa sổ 3
uv run jobcrawler api --reload       # cửa sổ 4: http://127.0.0.1:8000/api/docs
```

Postgres / Redis lấy từ docker compose ở gốc repo (cổng 15432 / 16379 trên 127.0.0.1, khớp
`DATABASE_URL` / `REDIS_URL` trong `.env.example`).

## Cấu hình

- Đọc từ biến môi trường, rồi file `.env` trong thư mục đang chạy (= `backend/`); biến môi trường
  thắng `.env`. Danh sách đủ kèm giải thích: `.env.example`.
- Phần lớn cài đặt sửa được ở trang Cài đặt của giao diện (lưu trong Postgres, đè lên env). Kết
  nối, bí mật hạ tầng, tên queue, gateway, đăng nhập, log chỉ đặt bằng env (`ENV_ONLY` trong
  `jobcrawler/services/app_settings.py`).
- `PROXY_MODE=central`: danh sách proxy chung ở `config/proxies.txt` (`PROXIES_FILE`, mẫu
  `config/proxies.example.txt`) và/hoặc trang Cài đặt.
- API sau reverse proxy (nginx của giao diện): `jobcrawler api` bật `proxy_headers` của uvicorn;
  IP thật của client (dùng để chặn đoán mật khẩu) chỉ được lấy từ `X-Forwarded-For` khi IP của
  proxy nằm trong `--forwarded-allow-ips` hoặc biến môi trường `FORWARDED_ALLOW_IPS` (uvicorn đọc
  từ môi trường process: `.env` chỉ có tác dụng khi compose nạp nó làm `env_file`; mặc định
  `127.0.0.1,::1`; nhận cả CIDR).
- Đăng nhập HTTP Basic bằng `UI_USERNAME` / `UI_PASSWORD`; để trống mật khẩu thì không hỏi (chỉ
  dùng khi cổng chỉ mở ở 127.0.0.1).

## Kiểm tra mã

```bash
uv run ruff format --check . && uv run ruff check .
uv run pyright
uv run pytest -m "not integration"   # không cần Postgres / Redis
uv run pytest                        # cả test tích hợp
```

Test tích hợp cần Postgres + Redis thật và `TEST_DATABASE_URL` (env hoặc `.env`) trỏ tới một DB
riêng có tên kết thúc bằng `_test`: schema ở đó bị xoá và dựng lại. Thiếu thì các test này bị bỏ
qua (trên CI, `CI=true`, thì là lỗi).

`task check` chạy đủ: lint, pyright, kiểm tra spec OpenAPI, pytest.

## Spec OpenAPI

`openapi.json` (commit ở đây, khoá sắp xếp) là hợp đồng API ↔ giao diện: frontend sinh hàm + kiểu
TypeScript từ `../backend/openapi.json`. Sửa API thì ghi lại spec rồi commit cùng mã:

```bash
uv run python scripts/export_openapi.py           # ghi lại (task api)
uv run python scripts/export_openapi.py --check   # chỉ kiểm tra (task api:check, test_openapi.py)
```

## Migration

```bash
uv run jobcrawler db upgrade                       # task migrate
uv run jobcrawler db revision -m "mô tả"           # task migration NAME="mô tả"
```

## Docker

Build context là `backend/`, cộng một build context tên `proto` trỏ tới `../proto` (image cài gói
hợp đồng từ đó). Từ `backend/`:

```bash
docker build --build-context proto=../proto -t jobcrawler-backend .
```

Từ gốc repo: `docker build --build-context proto=./proto -t jobcrawler-backend backend`; trong
compose: `build: {context: ./backend, additional_contexts: {proto: ./proto}}`. Lệnh mặc định của
image là `jobcrawler --help`; mỗi service compose đặt lệnh của vai mình (`jobcrawler api --host
0.0.0.0`, `jobcrawler gateway`, ...). Image chạy bằng user `app` (uid 1000), mở cổng 8000 và 50051.

## Thử tải

`scripts/loadtest/`: site careers giả (`sla_site.py`, `fake_sites.py`), báo cáo độ trễ phát hiện
tin mới (`sla_report.py`), dữ liệu lớn giả lập + đo truy vấn của giao diện (`seed.py`, `bench.py`).
Cách dùng ở docstring đầu mỗi file; chạy bằng `uv run python scripts/loadtest/<file>.py` trong
`backend/`.
