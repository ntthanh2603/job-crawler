# job-crawler — ghi chú cho agent

Chỉ ghi điều riêng của repo này mà agent dễ làm sai. Tổng quan: [README.md](README.md); kiến trúc:
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md); vận hành: [docs/OPERATIONS.md](docs/OPERATIONS.md);
triển khai: [docs/DEPLOY.md](docs/DEPLOY.md).

## Luật cứng (đọc trước khi sửa gì)

1. **Commit thẳng lên `main`**, không tạo nhánh phụ / PR. Message theo Conventional Commits
   (`feat(ui): …`, `fix: …`, `docs: …`, `refactor(gateway): …`); tiến độ ghi trong commit message
   (không có PROGRESS.md).
2. **Chạy `task check` trước khi commit** (đúng những gì CI chạy). Lệnh hay dùng mà chưa có thì
   thêm vào `Taskfile.yml`.
3. **Không sửa tay mã sinh từ proto** (`proto/jcproto/v1/*_pb2*.py[i]`): sửa `crawl.proto` rồi
   `task proto`, commit cả `.proto` lẫn mã sinh. Tương tự **spec API** (`frontend/openapi.json`,
   `frontend/src/lib/gen/` do orval sinh): sửa mô hình trả về (`...Out` trong `web/api.py`,
   `web/settings_api.py`) rồi `task api`, commit cả hai.
4. **Schema chỉ đổi bằng revision Alembic mới** (`task migration NAME="mô tả"` =
   `jobcrawler db revision`), xem lại file sinh ra trước khi commit. Không sửa revision đã áp dụng.
5. **Test không gọi mạng ngoài**: nguồn dùng `jobcrawler.fakes.FakeFetcher`, LLM dùng
   `FakeChatClient`, worker dùng gateway giả (`worker/tests/worker_fakes.py`) + server HTTP cục bộ.
6. **Không commit bí mật**: `.env`, `deploy/*.env`, file proxy (`proxies.txt`,
   `config/worker-proxies.txt`), chứng chỉ / khoá TLS.
7. **Giao diện web không bao giờ hiện lệnh shell / hướng dẫn vận hành** — chỉ trạng thái và cài đặt.
   Lưu ý vận hành ghi vào `docs/OPERATIONS.md` (hoặc `docs/DEPLOY.md`).
8. **Đổi hành vi thì cập nhật tài liệu cùng commit**: `docs/ARCHITECTURE.md`,
   `docs/OPERATIONS.md`, `docs/DEPLOY.md` (và `.env.example` khi thêm biến môi trường).
9. **Hỏi trước khi** thêm / nâng dependency (`pyproject.toml`, `uv.lock`, `package.json`) hoặc sửa
   CI (`.github/workflows/`).
10. **`.claude/skills/*` là bên thứ ba, ghim hash trong `skills-lock.json`**: không sửa.

## Cấu trúc repo

| Thư mục                | Nội dung                                                                              |
| ---------------------- | ------------------------------------------------------------------------------------- |
| `backend/jobcrawler/`  | API + giao diện (`web/`), gateway gRPC (`gateway/`), runner / scheduler, DB (`db/`), pipeline, nguồn (`sources/`), CLI (`entrypoints/cli.py`) |
| `worker/jobworker/`    | worker lấy trang (curl_cffi + Chromium, proxy riêng), gọi gateway qua gRPC             |
| `proto/jcproto/v1/`    | hợp đồng gRPC `crawl.proto` + mã sinh sẵn                                              |
| `frontend/`            | React + Vite + Tailwind + shadcn/ui; build ra `backend/jobcrawler/web/dist`            |
| `deploy/`              | compose cho máy backend và máy worker riêng                                           |
| `docs/`                | ARCHITECTURE, OPERATIONS, DEPLOY, BUILD_PROMPT (đặc tả gốc)                            |
| `scripts/`             | `gen_proto.py`, `loadtest/` (seed, bench, site giả đo SLA)                            |

Workspace uv một `.venv` cho ba gói (`jobcrawler`, `jobworker`, `jobcrawler-proto`). Node theo
`.nvmrc`.

## Lệnh chính (`task`, từ gốc repo)

```bash
task setup            # uv sync + npm ci + tạo .env từ .env.example nếu chưa có
task infra            # chỉ postgres + redis (chờ healthy)
task up / task down   # cả stack docker compose (up có build image)
task migrate          # jobcrawler db upgrade
task migration NAME="thêm cột x"
task fmt              # ruff format + ruff check --fix
task lint             # ruff format --check, ruff check, tsc frontend
task typecheck        # pyright + tsc
task test             # pytest (cả tích hợp nếu có DB test); tham số thêm sau `--`
task test:unit        # bỏ test tích hợp
task proto / task proto:check
task api / task api:check   # spec OpenAPI → frontend/openapi.json → orval → src/lib/gen/
task ui:dev / task ui:build / task ui:test
task check            # mọi thứ CI chạy
```

Hook git không cài sẵn; muốn dùng: `uvx pre-commit install -t pre-commit -t commit-msg`
(ruff, gitleaks, file lớn, proto, Conventional Commits).

## Test

- Unit: không cần gì ngoài `.venv` (`task test:unit`). Marker `browser` cần
  `playwright install chromium`, không có thì tự bỏ qua.
- Tích hợp (`-m integration`, `backend/tests/integration/`): cần Postgres + Redis (`task infra`) và
  `TEST_DATABASE_URL` trỏ tới DB **tên kết thúc `_test`**, khác `DATABASE_URL` — schema ở đó bị xoá
  và dựng lại. Compose tạo sẵn `jobcrawler_test` khi volume còn trống.
- Các bộ test tích hợp dùng chung `jobcrawler_test`: không chạy hai bộ song song.
- `worker/tests` không phải package (không có `__init__.py`) để không trùng tên `tests` với backend.

## Gotchas

1. **Mọi request tới website đi qua worker** (gateway gRPC + Redis Streams). Backend không tự tải
   trang; chỉ SERP / LLM gọi thẳng từ backend. Code nguồn mới dùng `HttpClient` / fetcher, không
   gọi `httpx` trực tiếp.
2. **Tên biến môi trường**: backend dùng `GATEWAY_*` (gateway + cấu hình đẩy xuống worker như
   `GATEWAY_HTTP_SLOTS`); worker dùng `WORKER_*` và `GATEWAY_ADDR` / `GATEWAY_TOKEN` /
   `GATEWAY_TLS` / `GATEWAY_CA_CERT`. Hai bên đọc chung `.env` nên tên mới không được trùng nghĩa.
3. **Cài đặt sửa ở giao diện đè `.env`** (bảng `app_settings`, nạp lại ≤ 5 s). Mục `live=False` cần
   khởi động lại process; kết nối / bí mật / gateway / đăng nhập là `ENV_ONLY`, không trả ra API.
4. **Cron** (`MAINTENANCE_CRON`, `BOARD_INDEX_CRON`, `BOARD_TAIL_CRON`…) và khung build / poll tính
   theo `CRAWL_TIMEZONE` (Asia/Ho_Chi_Minh), không phải UTC. Windows cần gói `tzdata` (đã có).
5. **Image Docker phải `--reinstall-package`** gói workspace: cache của uv cho gói cục bộ chỉ theo
   `pyproject.toml`, bỏ cờ này thì image mang mã cũ. Lệnh CLI mới (vd. `gateway-health` dùng làm
   healthcheck) chỉ có sau khi build lại image.
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
11. **Mã sinh proto phụ thuộc bản `grpcio-tools`** trong `uv.lock`: nâng bản thì `task proto` rồi
    commit lại, không thì `proto:check` (CI) đỏ. ruff / pyright bỏ qua file `*_pb2*`.
12. **Test tích hợp chập chờn trên Windows** (`WinError 64` khi nối Redis): chạy lại riêng file đó.
13. **Endpoint mới trả mô hình `...Out`** (gốc `ApiOut`), không trả `dict`: test bắt endpoint JSON
    không có mô hình. Mô hình chép từ dataclass của services thì thêm vào `MIRRORS` trong
    `tests/test_openapi.py`. Trường lấy từ Redis / JSON tự do (không có ràng buộc ở DB) đừng kiểm
    bằng enum / `Literal`: giá trị lạ làm endpoint lỗi 500 — dùng `known_values(...)` để spec vẫn
    liệt kê giá trị.
14. **Giao diện gọi API qua hàm / hook orval sinh** (`@/lib/gen/queries`, như open-asm), không
    `fetch` tay. orval không mã hoá tham số path: id tự do phải `encodeURIComponent`.
    openapi-typescript / hey-api không chạy được với TypeScript 7 (cần API compiler của TS 5).
