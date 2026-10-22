# job-crawler — ghi chú cho agent

Chỉ ghi điều riêng của repo này mà agent dễ làm sai. Tổng quan: [README.md](README.md); kiến trúc:
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); vận hành: [docs/OPERATIONS.md](docs/OPERATIONS.md);
triển khai: [docs/DEPLOY.md](docs/DEPLOY.md).

## Luật cứng (đọc trước khi sửa gì)

1. **Commit thẳng lên `main`**, không tạo nhánh phụ / PR. Message theo Conventional Commits
   (`feat(ui): …`, `fix: …`, `docs: …`, `refactor(gateway): …`); tiến độ ghi trong commit message
   (không có PROGRESS.md).
2. **Chạy `task check` trước khi commit** (đúng những gì CI chạy; chỉ sửa một service thì
   `task <service>:check` là đủ). Lệnh hay dùng mà chưa có thì thêm vào `Taskfile.yml` của
   service đó.
3. **Không sửa tay mã sinh từ proto** (`proto/jcproto/v1/*_pb2*.py[i]`): sửa `crawl.proto` rồi
   `task proto:gen`, commit cả `.proto` lẫn mã sinh. Tương tự **spec API** (`backend/openapi.json`,
   `frontend/src/lib/gen/` do orval sinh): sửa mô hình trả về (`...Out` trong
   `modules/*/*_schema.py`) rồi `task api`, commit cả hai.
4. **Schema chỉ đổi bằng revision Alembic mới** (`task backend:migration NAME="mô tả"` =
   `jobcrawler db revision`), xem lại file sinh ra trước khi commit. Không sửa revision đã áp dụng.
5. **Test không gọi mạng ngoài**: nguồn dùng `jobcrawler.services.fakes.FakeFetcher`, LLM dùng
   `FakeChatClient`, worker dùng gateway giả (`worker/tests/worker_fakes.py`) + server HTTP cục bộ.
6. **Không commit bí mật**: mọi `.env` (gốc, `backend/`, `worker/`), file proxy
   (`proxies.txt`, `*/config/*proxies*.txt` trừ file `.example`), chứng chỉ / khoá TLS.
7. **Giao diện web không bao giờ hiện lệnh shell / hướng dẫn vận hành** — chỉ trạng thái và cài đặt.
   Lưu ý vận hành ghi vào `docs/OPERATIONS.md` (hoặc `docs/DEPLOY.md`). Ngoại lệ duy nhất: lệnh
   `docker pull` + `docker run` chép được trong hộp thoại "Thêm worker" (kèm khoá tham gia).
8. **Đổi hành vi thì cập nhật tài liệu cùng commit**: `docs/ARCHITECTURE.md`,
   `docs/OPERATIONS.md`, `docs/DEPLOY.md` (và `.env.example` của service khi thêm biến môi
   trường).
9. **Hỏi trước khi** thêm / nâng dependency (`pyproject.toml`, `uv.lock`, `package.json` của một
   service) hoặc sửa CI (`.github/workflows/`).
10. **`.claude/skills/*` là bên thứ ba, ghim hash trong `skills-lock.json`**: không sửa.
11. **Mỗi service tự đủ**: không import chéo giữa `backend/` và `worker/` (chỉ cùng dùng `proto/`),
    không đọc / ghi file của service khác lúc chạy, không đặt lại file Python / Node ở gốc repo.
12. **Hợp đồng không phá vỡ**: `crawl.proto` chỉ thêm field / message, không đổi số hay xoá
    (`buf breaking` trong CI chặn); lệnh `jobcrawler serve`, `jobcrawler health` /
    `gateway-health`, `jobworker health` là hợp đồng với healthcheck — đổi thì sửa cùng
    `docker-compose.yaml`.
13. **Cache mới đặt ở Redis cache** (`res.cache_redis` / `cache_kv`, `CACHE_REDIS_URL`): dữ liệu
    mất được. Hàng đợi, khoá, rate limit, trạng thái phải giữ ở `res.redis` (`REDIS_URL`).

## Cấu trúc repo

| Thư mục                | Nội dung                                                                              |
| ---------------------- | ------------------------------------------------------------------------------------- |
| `backend/`             | service backend (gói `jobcrawler`, một image): chia như `core-api/src` của open-asm: `bootstrap/` (CLI `cli.py`, app FastAPI `app.py`, process gateway / runner / scheduler), `modules/<tính năng>/` (`<module>_router.py`, `<module>_schema.py`, `<module>_service.py`, `<tên>_service.py`), `services/` (hạ tầng: Redis, HTTP / proxy, LLM), `database/` (model, Alembic), `common/`, `pipeline/`, `sources/`; `scripts/` (export OpenAPI, loadtest); `openapi.json` |
| `worker/`              | service worker (gói `jobworker`): lấy trang (curl_cffi + Chromium, proxy riêng), gọi gateway qua gRPC; chia như backend: `bootstrap/` (CLI `cli.py`, process `run.py`), `modules/` (`gateway/` vòng Register → Lease → Complete, `fetch/` chạy việc HTTP / render, `health/`), `services/` (kênh gRPC, token, proxy tại worker), `common/`; `config/` proxy mẫu |
| `frontend/`            | service web: React + Vite + Tailwind + shadcn/ui, image nginx (`nginx/`) phục vụ bản build, chuyển `/api` và gRPC của worker tới backend (cổng duy nhất mở ra ngoài) |
| `proto/`               | thư viện hợp đồng gRPC dùng chung: `jcproto/v1/crawl.proto` + mã sinh sẵn, `scripts/gen.py` |
| `deploy/`              | script khởi tạo Postgres của compose (`postgres/init/`: DB `jobcrawler_test`)          |
| `docs/`                | ARCHITECTURE, OPERATIONS, DEPLOY                                                      |

Mỗi service có trong thư mục của nó: mã, `pyproject.toml` + `uv.lock` + `.python-version` (hoặc
`package.json` + lock + `.nvmrc`), cấu hình ruff / pyright / pytest, `Dockerfile` + `.dockerignore`,
`Taskfile.yml`, `.env.example`, README. Lệnh của một service chạy với thư mục làm việc là thư mục đó
(`cd backend && uv run …`), `.venv` và `.env` cũng ở đó. Backend / worker dùng `proto/` qua
`jobcrawler-proto = { path = "../proto", editable = true }`; Dockerfile của chúng nhận `proto/` là build
context tên `proto` (`docker build --build-context proto=proto backend`).

Ở gốc repo chỉ còn phần điều phối: `docker-compose.yaml`, `docker-compose.prod.yaml`, `Taskfile.yml`
(include Taskfile từng service), `.github/`,
`.pre-commit-config.yaml`, README / AGENTS / CLAUDE. Không có `pyproject.toml` / `uv.lock` /
`package.json` ở gốc.

## Lệnh chính (`task`, từ gốc repo)

```bash
task setup                    # dependency mọi service + backend/.env, worker/.env từ .env.example
task infra                    # chỉ postgres + redis (chờ healthy)
task up / task down           # 5 container: postgres, redis, backend, frontend, worker (up build 3 image)
task check                    # mọi thứ CI chạy (proto, backend, worker, frontend)
task fmt / lint / typecheck / test   # trên mọi service
task <service>:check          # một service: proto, backend, worker, frontend
task backend:test:unit        # pytest backend bỏ test tích hợp; tham số thêm sau `--`
task backend:migrate / task backend:migration NAME="thêm cột x"
task backend:dev / worker:dev / frontend:dev
task backend:cli -- <lệnh jobcrawler>   # vd. task backend:cli -- import companies.csv
task proto:gen / task proto:gen:check
task api / task api:check     # mô hình API → backend/openapi.json → orval → frontend/src/lib/gen/
task frontend:e2e             # Playwright trên stack đang chạy (127.0.0.1:8000)
task release VERSION=X.Y.Z    # tag + đẩy; CI build và đẩy image lên GHCR (không tự chạy)
```

Hook git không cài sẵn; muốn dùng: `uvx pre-commit install -t pre-commit -t commit-msg`
(ruff, gitleaks, file lớn, proto, Conventional Commits).

## Test

- Unit: không cần gì ngoài `.venv` của service (`task backend:test:unit`, `task worker:test`).
  Marker `browser` (worker) cần `playwright install chromium`, không có thì tự bỏ qua.
- Tích hợp (`-m integration`, `backend/tests/integration/`): cần Postgres + Redis (`task infra`) và
  `TEST_DATABASE_URL` trỏ tới DB **tên kết thúc `_test`**, khác `DATABASE_URL` — schema ở đó bị xoá
  và dựng lại. Compose tạo sẵn `jobcrawler_test` khi volume còn trống. **Không chạy ở CI / `task
  check`**: chạy `task backend:test` khi sửa phần DB, Redis, hàng đợi, gateway.
- Các bộ test tích hợp dùng chung `jobcrawler_test`: không chạy hai bộ song song.
- `.env` của test tích hợp đọc từ thư mục làm việc: chạy pytest backend trong `backend/` (đọc
  `backend/.env`).

## Gotchas

1. **Mọi request tới website đi qua worker** (gateway gRPC + Redis Streams). Backend không tự tải
   trang; chỉ SERP / LLM gọi thẳng từ backend. Code nguồn mới dùng `HttpClient` / fetcher, không
   gọi `httpx` trực tiếp.
2. **Tên biến môi trường**: backend dùng `GATEWAY_*` (gateway + cấu hình đẩy xuống worker như
   `GATEWAY_HEARTBEAT_SECONDS`); worker dùng `WORKER_*` (slot, giả lập trình duyệt, proxy riêng: chỉ
   đặt ở worker, trong lệnh khởi tạo) và `GATEWAY_ADDR` / `GATEWAY_TOKEN` /
   `GATEWAY_TLS` / `GATEWAY_CA_CERT`. Mỗi bên đọc `.env` của thư mục mình (`backend/.env`,
   `worker/.env`); `GATEWAY_TOKEN` phải giống nhau ở hai file.
3. **Cài đặt nằm trong DB** (bảng `app_settings`, trang Cài đặt, đè `.env`, nạp lại ≤ 5 s); `.env`
   chỉ cho `ENV_ONLY` (kết nối / bí mật hạ tầng / gateway / đăng nhập / log, không trả ra API).
   Mục `live=False` đọc lúc khởi động: `jobcrawler serve` tự khởi động lại process con khi chúng
   đổi (bộ đếm Redis `settings:restart`); cài đặt mới thêm vào `GROUPS` của
   `modules/settings/app_settings_service.py`, không thêm vào `backend/.env.example`.
4. **Cron** (`MAINTENANCE_CRON`, `BOARD_INDEX_CRON`, `BOARD_TAIL_CRON`…) và khung build / poll tính
   theo `CRAWL_TIMEZONE` (Asia/Ho_Chi_Minh), không phải UTC. Windows cần gói `tzdata` (đã có).
5. **Image Docker phải `--reinstall-package`** chính service và `jobcrawler-proto`: cache của uv cho
   gói cục bộ chỉ theo `pyproject.toml`, bỏ cờ này thì image mang mã cũ. Lệnh CLI mới (vd.
   `gateway-health` dùng làm healthcheck) chỉ có sau khi build lại image. Image chạy chép venv của
   stage `deps` rồi `/layer` (chỉ file của gói cục bộ): đừng chép cả `/opt/venv` của stage `build`,
   không thì mỗi lần sửa code lại đẩy / kéo cả venv vài trăm MB.
6. **Log không kèm locals**: traceback render với `show_locals=False`, Typer
   `pretty_exceptions_show_locals=False`. Bật Rich / locals làm mỗi lần log lỗi tốn 1–5 s trên event
   loop và có thể lộ token.
7. **Windows console không phải UTF-8**: entry point mới phải `reconfigure(encoding="utf-8")`
   stdout/stderr như `cli.main()` / `logging.py`, không thì tên doanh nghiệp có dấu làm crash.
8. **Postgres / Redis chỉ mở ở `127.0.0.1` (IPv4)**. Trên Windows `localhost` thử `::1` trước, bị
   từ chối sau ~2 s cho mỗi kết nối mới → URL nên dùng `127.0.0.1`.
9. **Healthcheck gateway** là `jobcrawler gateway-health` (grpc.health.v1, không cần token; TLS theo
   `GATEWAY_TLS_CERT/KEY`), một phần healthcheck của container `backend` (worker chờ nó healthy);
   chứng chỉ không có tên `127.0.0.1` thì healthcheck cần `--server-name`.
10. **Migration tạo extension** `pg_trgm`, `unaccent`: user DB cần quyền tạo extension (Postgres
    quản lý: tạo trước). Revision đánh số tiếp `0002`, `0003`…
11. **Mã sinh proto phụ thuộc bản `grpcio-tools`** trong `proto/uv.lock` và cần `protobuf` cùng
    major lúc chạy (`protobuf>=7.35.1,<8`): nâng bản thì `task proto:gen` rồi commit lại, không thì
    `proto:gen:check` (CI) đỏ. ruff / pyright bỏ qua file `*_pb2*`.
12. **Test tích hợp chập chờn trên Windows** (`WinError 64` khi nối Redis): chạy lại riêng file đó.
13. **Endpoint mới trả mô hình `...Out`** (gốc `ApiOut` ở `common/api.py`), không trả `dict`: test
    bắt endpoint JSON không có mô hình. Endpoint đặt ở `modules/<tính năng>/<module>_router.py`, mô
    hình ở `<module>_schema.py` cùng module; router mới phải ghi vào `ROUTERS` của
    `bootstrap/app.py` và có nhóm riêng ở `/api/docs` (`APIRouter(tags=["<nhóm>"])` + mô tả trong
    `OPENAPI_TAGS`, test kiểm). Endpoint danh sách nhận `Annotated[XxxQuery, Query()]` kế thừa
    `GetManyQuery` (`sortBy` là `Literal` cột được phép) và trả `PageOut` qua `page_out(...)`, như
    open-asm. Mô hình chép từ dataclass của service thì thêm vào `MIRRORS` trong
    `tests/test_openapi.py`. Trường lấy từ Redis / JSON tự do (không có ràng buộc ở DB) đừng kiểm
    bằng enum / `Literal`: giá trị lạ làm endpoint lỗi 500 — dùng `known_values(...)` để spec vẫn
    liệt kê giá trị.
14. **Giao diện gọi API qua hàm / hook orval sinh** (`@/lib/gen/queries`, như open-asm), không
    `fetch` tay. orval không mã hoá tham số path: id tự do phải `encodeURIComponent`.
    openapi-typescript / hey-api không chạy được với TypeScript 7 (cần API compiler của TS 5).
15. **Đăng nhập là JWT** (`modules/auth/auth_service.py`): test gọi API có `UI_PASSWORD` thì tự ký token
    (`app.state.token_auth.issue()`) và gửi `Authorization: Bearer` — không còn HTTP Basic.
    Trường bí mật / proxy của `app_settings` lưu mã hoá (`secretbox`, `app_settings_service.SENSITIVE`):
    đọc bảng thẳng thì giải mã bằng `secretbox.current()`; thêm cài đặt bí mật mới (kind `secret`)
    là tự được mã hoá.
16. **Worker xác thực bằng token riêng** (`modules/gateway/worker_auth_service.py`): `GATEWAY_TOKEN` / khoá một lần
    chỉ để `Register`; RPC khác cần `worker_token` đúng `worker_id`. Worker giả trong test gateway
    phải đổi metadata sang `RegisterResponse.worker_token` sau khi đăng ký.
17. **Khoá phân tán dùng `locks.RedisLease`** (scheduler leader, bảo trì), không dùng khoá advisory
    theo phiên Postgres mới (hỏng sau PgBouncer chế độ transaction).
18. **Đổi `jobs` ở đâu cũng phải phát sự kiện** (`store.emit_job_events`, cùng transaction): hệ
    thống ngoài nhận việc làm qua outbox `job_events` (docs/ARCHITECTURE.md mục 12). Ghi đè nội
    dung job từ chỗ mới (bảo trì, gộp…) thì phát `updated` và đặt `content_hash = NULL`; đóng thì
    `closed`; xoá dòng thì `deleted`. Không phát thì đối tác không bao giờ thấy thay đổi đó.
19. **API tích hợp (`/api/integration/v1`) chỉ nhận khoá API** (`modules/integration/key_service.py`,
    mọi chế độ đăng nhập); test gọi nó tạo client + khoá (`/api/api-clients/...`) rồi gửi
    `Authorization: Bearer jca_…`. Giao diện không có hàm orval cho các đường này; hợp đồng cho đối
    tác là docs/INTEGRATION.md — đổi API thì sửa cùng file đó.
