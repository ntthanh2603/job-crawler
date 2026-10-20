# jobcrawler (backend)

Service backend của jobcrawler: gói Python `jobcrawler`, một image Docker. Container `backend` chạy
`jobcrawler serve` (migration rồi bốn process api, gateway, scheduler, runner); từng vai vẫn chạy
riêng được bằng lệnh của nó. Không có Chromium / curl_cffi: mọi request tới website đi qua **worker** lấy trang (`../worker`)
qua gateway gRPC. Giao diện React là service riêng (`../frontend`, nginx) gọi API này qua `/api`.

| Lệnh                                  | Vai                                                                    |
| ------------------------------------- | ---------------------------------------------------------------------- |
| `jobcrawler serve`                    | cả backend: migration rồi api + gateway + scheduler + runner (Docker)  |
| `jobcrawler api`                      | API JSON (cổng 8000): `/api/...`, `/api/docs`, `/healthz`              |
| `jobcrawler gateway`                  | gateway gRPC cho worker (cổng 50051); healthcheck `gateway-health`     |
| `jobcrawler scheduler`                | xếp lịch build bộ khung ban đêm, poll tin mới ban ngày                 |
| `jobcrawler runner --role build`      | job trong hàng đợi Redis Streams; vai `build`, `poll`, `boards`        |
| `jobcrawler db upgrade`               | migrate schema Postgres (Alembic), chạy một lần trước các vai khác     |
| `jobcrawler health <component>...`    | healthcheck của scheduler / runner trong container (xem dưới)          |

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
cp .env.example .env                 # kết nối, gateway, đăng nhập; còn lại ở trang Cài đặt
uv run jobcrawler db upgrade
uv run jobcrawler gateway            # cửa sổ 1 (worker nối vào 127.0.0.1:50051)
uv run jobcrawler runner             # cửa sổ 2
uv run jobcrawler scheduler          # cửa sổ 3
uv run jobcrawler api --reload       # cửa sổ 4: http://127.0.0.1:8000/api/docs
```

Postgres / Redis lấy từ docker compose ở gốc repo (cổng 15432 / 16379 trên 127.0.0.1, khớp
`DATABASE_URL` / `REDIS_URL` trong `.env.example`).

## Cấu hình

- Cài đặt nằm trong Postgres (bảng `app_settings`), sửa ở trang Cài đặt của giao diện (danh mục,
  giải thích, kiểm tra: `jobcrawler/services/app_settings.py`). `.env` (`.env.example`) chỉ cho
  kết nối, bí mật hạ tầng, tên queue, gateway, đăng nhập, watchdog, log (`ENV_ONLY`).
- Mọi field vẫn đọc được từ biến môi trường, rồi file `.env` trong thư mục đang chạy (=
  `backend/`), rồi file bí mật (dưới); giá trị trong DB thắng. Trang Cài đặt chép giá trị đặt bằng
  biến môi trường vào DB (`POST /api/settings/import-env`).
- Cài đặt chỉ đọc lúc khởi động (`live=False`): `jobcrawler serve` tự khởi động lại các process
  con khi lưu đổi chúng; chạy từng lệnh riêng thì tự khởi động lại.
- Danh sách proxy chung: bảng proxy của trang Cài đặt (cài đặt `proxies`, trong DB, mã hoá) và/hoặc
  `config/proxies.txt` (`PROXIES_FILE`, mẫu `config/proxies.example.txt`). `PROXY_MODE=worker`:
  gateway gửi danh sách xuống worker trong `WorkerConfig.shared_proxies` (`gateway/server.py`),
  worker tự xoay; `PROXY_MODE=central`: backend chọn cho từng request. `GET /api/proxies`: trạng
  thái từng proxy; `POST /api/proxies/check`: kiểm tra qua worker (`jobcrawler/services/proxies.py`).
- API sau reverse proxy (nginx của giao diện): `jobcrawler api` bật `proxy_headers` của uvicorn;
  IP thật của client (dùng để chặn đoán mật khẩu) chỉ được lấy từ `X-Forwarded-For` khi IP của
  proxy nằm trong `--forwarded-allow-ips` hoặc biến môi trường `FORWARDED_ALLOW_IPS` (uvicorn đọc
  từ môi trường process: `.env` chỉ có tác dụng khi compose nạp nó làm `env_file`; mặc định
  `127.0.0.1,::1`; nhận cả CIDR).
- Đăng nhập (`jobcrawler/web/auth.py`): trang đăng nhập với `UI_USERNAME` / `UI_PASSWORD`,
  `POST /api/auth/login` trả JWT (HS256, ký bằng `JWT_SECRET`, trống = khoá tự sinh trên Redis) và
  đặt cookie httpOnly `jc_token`; script gọi API gửi `Authorization: Bearer <access_token>`.
  `POST /api/auth/logout` thu hồi phiên. Để trống mật khẩu thì không hỏi (chỉ dùng khi cổng chỉ
  mở ở 127.0.0.1). Hoặc SSO qua proxy (dưới).
- Trường nhạy cảm của trang Cài đặt (khoá API, proxy chung) lưu mã hoá AES-256-GCM
  (`jobcrawler/secretbox.py`, khoá `ENCRYPTION_KEYS`).
- Production: `LOG_DB_LEVEL=WARNING` (bảng `logs` chỉ giữ cảnh báo / lỗi), `LOG_FORMAT=json`.

### Bí mật từ file (Docker secrets)

Mỗi field đọc được từ một file cùng tên (viết thường) trong `SECRETS_DIR`: `gateway_token`,
`llm_api_key`, `serp_api_key`, `ui_password`, `database_url`... Không đặt
`SECRETS_DIR` thì dùng `/run/secrets` nếu thư mục đó tồn tại (chỗ compose gắn `secrets:`), nên
máy dev không bị ảnh hưởng; `SECRETS_DIR=` (rỗng) tắt hẳn. `SECRETS_DIR` chỉ đọc từ biến môi
trường của process. Thứ tự ưu tiên (pydantic-settings): tham số truyền vào `Settings(...)` > biến
môi trường > `.env` > file bí mật > mặc định; biến môi trường đặt rỗng bị bỏ qua nên file vẫn
được dùng (compose có thể để `GATEWAY_TOKEN: ${GATEWAY_TOKEN:-}` cạnh secret cùng tên).

### Redis: chính và cache

- `REDIS_URL` (chính, `noeviction` + lưu đĩa): hàng đợi job, stream / trả lời / registry /
  inflight của gateway, khoá doanh nghiệp, rate limit theo domain và của LLM, trạng thái proxy
  pool, phiên bản cài đặt, số đo SLA poll, chặn đoán mật khẩu, heartbeat health.
- `CACHE_REDIS_URL` (cache, đặt được `maxmemory` + `allkeys-lru`, không cần lưu đĩa): cache trang
  HTTP, robots.txt, kết quả tìm nguồn, SERP, câu trả lời LLM, số liệu trang Tổng quan (khoá
  `<REDIS_PREFIX>:http:…`, `robots:…`, `discovery:…`, `serp:…`, `llm:…`, `overview:…`). Mất khoá
  chỉ làm chậm / tốn thêm request. Trống = dùng chung REDIS_URL (hành vi như trước).
- Rate limit cố ý ở Redis chính: token bucket và lệnh tạm dừng domain sau 429 bị đẩy ra thì backend
  gửi dồn tới site.

### Đăng nhập SSO qua proxy

Đặt `AUTH_PROXY_USER_HEADER` (vd. `X-Auth-Request-User` của oauth2-proxy, có thể kèm
`AUTH_PROXY_EMAIL_HEADER`): API lấy người dùng từ header đó (log `actor`, nhật ký thao tác ghi
đúng người), không dùng trang đăng nhập, không chặn đoán mật khẩu; thiếu header → 401 JSON không
kèm `WWW-Authenticate`. Vẫn chặn request ghi từ site khác (CSRF). `AUTH_LOGOUT_URL` (vd.
`/oauth2/sign_out`) là nút đăng xuất của giao diện. `GET /api/me` trả `user`, `auth`
(`proxy` | `jwt` | `none`) và `logout_url` (null khi không đặt).

**Chỉ an toàn khi cổng api không ai gọi thẳng được**: mọi request phải đi qua oauth2-proxy → nginx
giao diện, proxy ghi đè header người dùng. Gọi thẳng được api thì ai cũng tự đặt header được.

## Health check

- `jobcrawler health <component>...` (healthcheck Docker, chạy trong chính container): exit 0 khi
  heartbeat của container này (khoá `<REDIS_PREFIX>:health:<component>:<hostname>` trên REDIS_URL,
  hostname = id container) mới hơn `--max-age` giây (mặc định 15 = 3 nhịp 5 giây), không thì exit
  1 kèm lý do; không đọc được Redis cũng exit 1. Component: `scheduler`, `runner-build`,
  `runner-poll`, `runner-boards`; `jobcrawler runner` đủ ba vai là `runner`. Nhiều component thì
  phải khoẻ cả (container `backend`: `jobcrawler health scheduler runner`). Heartbeat được ghi
  ngay trong vòng lặp chính (vòng lặp kẹt → heartbeat cũ dần).

### Watchdog

Docker không tự khởi động lại container `unhealthy`, nên `api`, `gateway`, `scheduler`, `runner` tự
canh mình (`jobcrawler/watchdog.py`): event loop không quay được quá `WATCHDOG_SECONDS` giây (mặc
định 120; `0` = tắt), hoặc vòng lặp chính (nhịp scheduler, vòng nhận việc của bất kỳ vai runner
nào) không quay vòng nào quá ngần ấy, thì log `watchdog.stall` (`what` = `event_loop` |
`main_loop`, kèm stack của event loop) rồi thoát với mã **70**; `jobcrawler serve` thấy process con
thoát thì dừng các process còn lại và thoát, `restart: unless-stopped` dựng lại container. Vòng
lặp lỗi vẫn tính là quay: mất Postgres / Redis không làm thoát (healthcheck vẫn báo hỏng như cũ). Dừng có chủ đích (SIGTERM, Ctrl+C) tắt watchdog trước khi dọn dẹp. Dự phòng khi mã C
giữ GIL làm luồng canh cũng đứng: `faulthandler` in traceback mọi luồng rồi thoát mã 1 sau
`WATCHDOG_SECONDS + 60` giây.

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
image là `jobcrawler --help`; compose chạy `jobcrawler serve --host 0.0.0.0`. Image chạy bằng user `app` (uid 1000), mở cổng 8000 và 50051.

## Thử tải

`scripts/loadtest/`: site careers giả (`sla_site.py`, `fake_sites.py`), báo cáo độ trễ phát hiện
tin mới (`sla_report.py`), dữ liệu lớn giả lập + đo truy vấn của giao diện (`seed.py`, `bench.py`).
Cách dùng ở docstring đầu mỗi file; chạy bằng `uv run python scripts/loadtest/<file>.py` trong
`backend/`.
