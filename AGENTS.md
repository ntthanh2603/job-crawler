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
   `frontend/src/lib/gen/` do orval sinh): sửa mô hình trả về (`...Out` trong `web/api.py`,
   `web/settings_api.py`) rồi `task api`, commit cả hai.
4. **Schema chỉ đổi bằng revision Alembic mới** (`task backend:migration NAME="mô tả"` =
   `jobcrawler db revision`), xem lại file sinh ra trước khi commit. Không sửa revision đã áp dụng.
5. **Test không gọi mạng ngoài**: nguồn dùng `jobcrawler.fakes.FakeFetcher`, LLM dùng
   `FakeChatClient`, worker dùng gateway giả (`worker/tests/worker_fakes.py`) + server HTTP cục bộ.
6. **Không commit bí mật**: mọi `.env` (gốc, `backend/`, `worker/`), `deploy/*.env`, file proxy
   (`proxies.txt`, `*/config/*proxies*.txt` trừ file `.example`), chứng chỉ / khoá TLS.
7. **Giao diện web không bao giờ hiện lệnh shell / hướng dẫn vận hành** — chỉ trạng thái và cài đặt.
   Lưu ý vận hành ghi vào `docs/OPERATIONS.md` (hoặc `docs/DEPLOY.md`).
8. **Đổi hành vi thì cập nhật tài liệu cùng commit**: `docs/ARCHITECTURE.md`,
   `docs/OPERATIONS.md`, `docs/DEPLOY.md` (và `.env.example` của service khi thêm biến môi
   trường).
9. **Hỏi trước khi** thêm / nâng dependency (`pyproject.toml`, `uv.lock`, `package.json` của một
   service) hoặc sửa CI (`.github/workflows/`).
10. **`.claude/skills/*` là bên thứ ba, ghim hash trong `skills-lock.json`**: không sửa.
11. **Mỗi service tự đủ**: không import chéo giữa `backend/` và `worker/` (chỉ cùng dùng `proto/`),
    không đọc / ghi file của service khác lúc chạy, không đặt lại file Python / Node ở gốc repo.

## Cấu trúc repo

| Thư mục                | Nội dung                                                                              |
| ---------------------- | ------------------------------------------------------------------------------------- |
| `backend/`             | service backend (gói `jobcrawler`, một image): API JSON (`jobcrawler/web/`), gateway gRPC (`gateway/`), runner / scheduler, DB (`db/`), pipeline, nguồn (`sources/`), CLI (`entrypoints/cli.py`); `scripts/` (export OpenAPI, loadtest); `openapi.json` |
| `worker/`              | service worker (gói `jobworker`): lấy trang (curl_cffi + Chromium, proxy riêng), gọi gateway qua gRPC; `config/` proxy mẫu |
| `frontend/`            | service web: React + Vite + Tailwind + shadcn/ui, image nginx (`nginx/`) phục vụ bản build và chuyển `/api` tới backend |
| `proto/`               | thư viện hợp đồng gRPC dùng chung: `jcproto/v1/crawl.proto` + mã sinh sẵn, `scripts/gen.py` |
| `deploy/`              | compose cho máy backend / máy worker riêng; script khởi tạo Postgres (`postgres/init/`) |
| `docs/`                | ARCHITECTURE, OPERATIONS, DEPLOY, BUILD_PROMPT (đặc tả gốc)                            |

Mỗi service có trong thư mục của nó: mã, `pyproject.toml` + `uv.lock` + `.python-version` (hoặc
`package.json` + lock + `.nvmrc`), cấu hình ruff / pyright / pytest, `Dockerfile` + `.dockerignore`,
`Taskfile.yml`, `.env.example`, README. Lệnh của một service chạy với thư mục làm việc là thư mục đó
(`cd backend && uv run …`), `.venv` và `.env` cũng ở đó. Backend / worker dùng `proto/` qua
`jobcrawler-proto = { path = "../proto", editable = true }`; Dockerfile của chúng nhận `proto/` là build
context tên `proto` (`docker build --build-context proto=proto backend`).

Ở gốc repo chỉ còn phần điều phối: `docker-compose.yaml`, `Taskfile.yml` (include Taskfile từng
service), `.env.example` (biến của compose: cổng, số bản, tài khoản Postgres), `.github/`,
`.pre-commit-config.yaml`, README / AGENTS / CLAUDE. Không có `pyproject.toml` / `uv.lock` /
`package.json` ở gốc.

## Lệnh chính (`task`, từ gốc repo)

```bash
task setup                    # dependency mọi service + .env, backend/.env, worker/.env từ .env.example
task infra                    # chỉ postgres + redis (chờ healthy)
task up / task down           # cả stack docker compose (up có build 3 image)
task check                    # mọi thứ CI chạy (proto, backend, worker, frontend)
task fmt / lint / typecheck / test   # trên mọi service
task <service>:check          # một service: proto, backend, worker, frontend
task backend:test:unit        # pytest backend bỏ test tích hợp; tham số thêm sau `--`
task backend:migrate / task backend:migration NAME="thêm cột x"
task backend:dev / worker:dev / frontend:dev
task backend:cli -- <lệnh jobcrawler>   # vd. task backend:cli -- import companies.csv
task proto:gen / task proto:gen:check
task api / task api:check     # mô hình API → backend/openapi.json → orval → frontend/src/lib/gen/
```

Hook git không cài sẵn; muốn dùng: `uvx pre-commit install -t pre-commit -t commit-msg`
(ruff, gitleaks, file lớn, proto, Conventional Commits).

## Test

- Unit: không cần gì ngoài `.venv` của service (`task backend:test:unit`, `task worker:test`).
  Marker `browser` (worker) cần `playwright install chromium`, không có thì tự bỏ qua.
- Tích hợp (`-m integration`, `backend/tests/integration/`): cần Postgres + Redis (`task infra`) và
  `TEST_DATABASE_URL` trỏ tới DB **tên kết thúc `_test`**, khác `DATABASE_URL` — schema ở đó bị xoá
  và dựng lại. Compose tạo sẵn `jobcrawler_test` khi volume còn trống.
- Các bộ test tích hợp dùng chung `jobcrawler_test`: không chạy hai bộ song song.
- `.env` của test tích hợp đọc từ thư mục làm việc: chạy pytest backend trong `backend/` (đọc
  `backend/.env`).

## Gotchas

1. **Mọi request tới website đi qua worker** (gateway gRPC + Redis Streams). Backend không tự tải
   trang; chỉ SERP / LLM gọi thẳng từ backend. Code nguồn mới dùng `HttpClient` / fetcher, không
   gọi `httpx` trực tiếp.
2. **Tên biến môi trường**: backend dùng `GATEWAY_*` (gateway + cấu hình đẩy xuống worker như
   `GATEWAY_HTTP_SLOTS`); worker dùng `WORKER_*` và `GATEWAY_ADDR` / `GATEWAY_TOKEN` /
   `GATEWAY_TLS` / `GATEWAY_CA_CERT`. Mỗi bên đọc `.env` của thư mục mình (`backend/.env`,
   `worker/.env`); `GATEWAY_TOKEN` phải giống nhau ở hai file.
3. **Cài đặt sửa ở giao diện đè `.env`** (bảng `app_settings`, nạp lại ≤ 5 s). Mục `live=False` cần
   khởi động lại process; kết nối / bí mật / gateway / đăng nhập là `ENV_ONLY`, không trả ra API.
4. **Cron** (`MAINTENANCE_CRON`, `BOARD_INDEX_CRON`, `BOARD_TAIL_CRON`…) và khung build / poll tính
   theo `CRAWL_TIMEZONE` (Asia/Ho_Chi_Minh), không phải UTC. Windows cần gói `tzdata` (đã có).
5. **Image Docker phải `--reinstall-package`** chính service và `jobcrawler-proto`: cache của uv cho
   gói cục bộ chỉ theo `pyproject.toml`, bỏ cờ này thì image mang mã cũ. Lệnh CLI mới (vd.
   `gateway-health` dùng làm healthcheck) chỉ có sau khi build lại image.
6. **Log không kèm locals**: traceback render với `show_locals=False`, Typer
   `pretty_exceptions_show_locals=False`. Bật Rich / locals làm mỗi lần log lỗi tốn 1–5 s trên event
   loop và có thể lộ token.
7. **Windows console không phải UTF-8**: entry point mới phải `reconfigure(encoding="utf-8")`
   stdout/stderr như `cli.main()` / `logging.py`, không thì tên doanh nghiệp có dấu làm crash.
8. **Postgres / Redis chỉ mở ở `127.0.0.1` (IPv4)**. Trên Windows `localhost` thử `::1` trước, bị
   từ chối sau ~2 s cho mỗi kết nối mới → URL nên dùng `127.0.0.1`.
9. **Healthcheck gateway** là `jobcrawler gateway-health` (grpc.health.v1, không cần token; TLS theo
   `GATEWAY_TLS_CERT/KEY`). Worker trong compose chờ gateway healthy; chứng chỉ không có tên
   `127.0.0.1` thì healthcheck cần `--server-name`.
10. **Migration tạo extension** `pg_trgm`, `unaccent`: user DB cần quyền tạo extension (Postgres
    quản lý: tạo trước). Revision đánh số tiếp `0002`, `0003`…
11. **Mã sinh proto phụ thuộc bản `grpcio-tools`** trong `proto/uv.lock` và cần `protobuf` cùng
    major lúc chạy (`protobuf>=7.35.1,<8`): nâng bản thì `task proto:gen` rồi commit lại, không thì
    `proto:gen:check` (CI) đỏ. ruff / pyright bỏ qua file `*_pb2*`.
12. **Test tích hợp chập chờn trên Windows** (`WinError 64` khi nối Redis): chạy lại riêng file đó.
13. **Endpoint mới trả mô hình `...Out`** (gốc `ApiOut`), không trả `dict`: test bắt endpoint JSON
    không có mô hình. Mô hình chép từ dataclass của services thì thêm vào `MIRRORS` trong
    `tests/test_openapi.py`. Trường lấy từ Redis / JSON tự do (không có ràng buộc ở DB) đừng kiểm
    bằng enum / `Literal`: giá trị lạ làm endpoint lỗi 500 — dùng `known_values(...)` để spec vẫn
    liệt kê giá trị.
14. **Giao diện gọi API qua hàm / hook orval sinh** (`@/lib/gen/queries`, như open-asm), không
    `fetch` tay. orval không mã hoá tham số path: id tự do phải `encodeURIComponent`.
    openapi-typescript / hey-api không chạy được với TypeScript 7 (cần API compiler của TS 5).
