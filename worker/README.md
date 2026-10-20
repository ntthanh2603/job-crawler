# jobworker — worker lấy trang

Process nhỏ, không giữ trạng thái, chạy ở máy nào cũng được (thường kèm proxy riêng). Worker kết
nối gateway gRPC của backend (`proto/jcproto/v1/crawl.proto`), xin việc, tải trang rồi trả nguyên
văn phản hồi. Một việc là **một** request HTTP (curl_cffi, impersonate Chrome) hoặc **một** lần mở
trang bằng Chromium (Playwright). Worker không parse, không retry, không đọc robots.txt, không rate
limit: toàn bộ logic thu thập nằm ở backend.

Vòng lặp: `Register` → `Lease` (long-poll, chỉ xin đúng số slot còn trống) → tải → `Complete` (gộp
lô) song song `Heartbeat` → `Leave` khi dừng. Gateway mất kết nối thì worker thử lại với backoff
(0.5 s → 30 s) mãi, không tự thoát; gateway quên worker (khởi động lại) thì worker tự đăng ký lại.

## Chạy

Worker là một service riêng: `pyproject.toml`, `uv.lock`, `.python-version`, `.venv` và
`Taskfile.yml` riêng, mọi lệnh chạy trong `worker/`. Thứ duy nhất lấy từ ngoài thư mục này là hợp
đồng gRPC `../proto` (gói `jobcrawler-proto`, cài dạng editable khi dev).

```bash
uv sync                     # tạo .venv kèm công cụ dev (hoặc `task setup`)
cp .env.example .env        # rồi sửa GATEWAY_ADDR, khoá (GATEWAY_TOKEN / WORKER_JOIN_KEY), proxy...
uv run jobworker run        # hoặc `task dev`
uv run jobworker check      # kết nối gateway + IP đi ra qua từng proxy tại worker
uv run jobworker health     # healthcheck: mã 0 = khoẻ, 1 = không (xem Healthcheck)
uv run jobworker version    # lệnh bất kỳ: `task cli -- version`
```

Docker: build context là `worker/`, cộng build context tên `proto` trỏ tới `proto/` (compose:
`additional_contexts: {proto: ./proto}`). Image không chứa `config/` hay `.env`: proxy mount lúc
chạy, biến truyền qua môi trường.

```bash
docker build --build-context proto=../proto -t jobcrawler-worker .
docker run --init --stop-timeout 40 --env-file .env \
  -e WORKER_PROXIES_FILE=/app/config/worker-proxies.txt -v ./config:/app/config:ro \
  jobcrawler-worker
```

Compose: đặt `init: true` (dọn process con của Chromium), `stop_grace_period` lớn hơn
`WORKER_SHUTDOWN_GRACE_SECONDS` vài giây, mount `./worker/config:/app/config:ro` và
`WORKER_PROXIES_FILE=/app/config/worker-proxies.txt`. Image có sẵn `HEALTHCHECK` (`jobworker
health`, xem Healthcheck); token và proxy nên đưa vào bằng Docker secrets (xem Secret).

Dừng (SIGTERM / Ctrl-C): thôi xin việc, chờ việc đang chạy tối đa `WORKER_SHUTDOWN_GRACE_SECONDS`
(kết quả vẫn được gửi), `Leave` rồi thoát. Tín hiệu lần hai: huỷ luôn việc đang chạy (backend nhận
lỗi và thử lại ngay). Gateway yêu cầu drain (trang Workers) thì worker làm nốt việc rồi thoát với
mã 0.

## Biến môi trường

Đọc từ môi trường, file `.env` ở thư mục chạy (`worker/.env`, mẫu ở `.env.example`) và thư mục
secret (xem Secret); thứ tự ưu tiên: môi trường > `.env` > file secret > mặc định. Biến trống coi
như không đặt.

| Biến | Mặc định | Ý nghĩa |
| --- | --- | --- |
| `GATEWAY_ADDR` | `localhost:50051` | Địa chỉ gateway gRPC (`host:port`): máy khác dùng host:cổng giao diện của máy backend (nginx chuyển tới gateway) |
| `GATEWAY_TOKEN` | — | Khoá tham gia cố định, khớp `GATEWAY_TOKEN` của backend: vào gateway lần đầu / khi token riêng hết hạn |
| `WORKER_JOIN_KEY` | — | Khoá tham gia dùng một lần (trang Workers → "Thêm worker", hết hạn sau 15 phút) |
| `WORKER_TOKEN_FILE` | `~/.jobworker/token` | Token riêng gateway cấp sau khi vào (`jobworker/credentials.py`); mount volume để giữ qua lần tạo lại container |
| `GATEWAY_TLS` | `false` | Kết nối TLS |
| `GATEWAY_CA_CERT` | — | File PEM của CA riêng (chứng chỉ tự ký); trống = CA hệ thống |
| `WORKER_ID` | `<hostname>-<8 hex>` | Id duy nhất mỗi process; mặc định sinh mới mỗi lần khởi động |
| `WORKER_POOL` | `default` | Nhóm worker (xem Pool bên dưới) |
| `WORKER_HTTP_SLOTS` | tự động | Request HTTP cùng lúc tối đa; trống = 8 mỗi CPU, trong [8, 64] (`jobworker/sysres.py`) |
| `WORKER_RENDER_SLOTS` | tự động | Trang Chromium cùng lúc tối đa; trống = 1 mỗi 768 MiB RAM, không quá số CPU, trong [1, 8]; `0` = không bao giờ khởi động Chromium |
| `WORKER_PROXIES` | — | Proxy tại worker: JSON list hoặc cách nhau bởi dấu phẩy / xuống dòng |
| `WORKER_PROXIES_FILE` | — | File proxy, định dạng như `config/proxies.example.txt` |
| `WORKER_IMPERSONATE` | `chrome` | Fingerprint curl_cffi |
| `WORKER_LABELS` | `{}` | Nhãn tự do, JSON (`{"region": "hn", "provider": "x"}`), hiện ở trang Workers |
| `WORKER_MAX_BODY_BYTES` | `10485760` | Body lớn hơn thì trả `ERROR_KIND_TOO_LARGE` (không kèm body) |
| `WORKER_SHUTDOWN_GRACE_SECONDS` | `30` | Chờ việc đang chạy khi dừng |
| `WORKER_PROXY_STICKY_SECONDS` | `600` | Giữ một proxy cho mỗi domain chừng này giây |
| `WORKER_PROXY_COOLDOWN_SECONDS` | `120` | Proxy nghỉ với domain sau lỗi kết nối / 403 / 407 / 429 |
| `WORKER_HEALTH_FILE` | `/tmp/jobworker-health.json` | File trạng thái cho `jobworker health` (Windows: `%TEMP%\jobworker-health.json`) |
| `WORKER_WATCHDOG_SECONDS` | `120` | Event loop / vòng heartbeat đứng quá chừng này giây thì thoát mã 70 (xem Watchdog); `0` = tắt, khác 0 thì ít nhất 10 |
| `SECRETS_DIR` | `/run/secrets` nếu có | Thư mục secret, mỗi file một biến (xem Secret); đặt mà không tồn tại thì worker không chạy |
| `LOG_LEVEL` | `INFO` | `DEBUG` in từng việc (`worker.task_done`) |
| `LOG_FORMAT` | `console` | `json`: mỗi dòng một event |

Gateway điều chỉnh lúc chạy (trang Cài đặt của backend → `WorkerConfig`): số slot HTTP/render là
**trần** — worker dùng `min(số của gateway, WORKER_HTTP_SLOTS / WORKER_RENDER_SLOTS)`, nên gateway
chỉ hạ được số slot của máy lớn, không nâng máy nhỏ lên. `impersonate`, giới hạn body, chu kỳ
heartbeat, thời gian long-poll, có dùng proxy tại worker hay không thì gateway ghi đè. Giá trị 0 /
trống giữ cấu hình của worker. Riêng `WORKER_RENDER_SLOTS=0` là tắt hẳn: gateway không bật render
lên được.

Không mở được Chromium (thiếu RAM, Chromium chết rồi mở lại không được...): việc render đó trả lỗi,
worker tạm tắt render (đăng ký lại với 0 slot render để backend không gửi việc render tới) trong
30 s, 60 s, 120 s... tối đa 10 phút theo số lần lỗi liên tiếp, hết hạn thì đăng ký lại với đủ slot
và thử mở lại; mở được thì đếm lại từ đầu. Máy không có Chromium (chưa `playwright install
chromium`) thì việc render đầu tiên trả `ERROR_KIND_UNSUPPORTED` và worker tắt render hẳn. Trang
Workers hiện lý do và giờ thử lại (`WorkerStats.render_disabled_reason` / `render_retry_at_ms`).

Gateway ghi lại việc worker đang giữ: kết quả của việc worker không còn giữ bị bỏ (`dropped`).
Worker rời (`Leave`) khi còn việc chưa trả, hoặc quá `GATEWAY_WORKER_TTL_SECONDS` không báo sống,
thì việc của nó được trả về backend ngay (`ERROR_KIND_WORKER_LOST`) để backend gửi lại cho worker
khác; kết quả tới muộn của worker đó bị bỏ.

Mỗi `Heartbeat` kể các việc worker còn nợ kết quả (`task_ids`: đang chạy, hoặc đã xong mà
`Complete` chưa được nhận). Gateway đối soát: việc đã giao hơn 60 s mà worker không kể (lời trả
`Lease` không tới worker, hoặc `Complete` thử lại hết lượt vẫn hỏng) được trả `ERROR_KIND_WORKER_LOST`
cho backend; việc worker kể mà gateway đã thu hồi (worker từng quá hạn báo sống) được báo lại trong
`revoked_task_ids` và worker huỷ ngay (`worker.task_revoked`).

Cấu hình đẩy xuống từ backend nằm ở các biến `GATEWAY_HTTP_SLOTS`, `GATEWAY_RENDER_SLOTS`,
`GATEWAY_IMPERSONATE`… trong `.env` của backend (hoặc trang Cài đặt), không trùng tên với biến
`WORKER_*` ở đây. Riêng `GATEWAY_TOKEN` (nếu worker vào bằng khoá cố định) phải đặt cùng giá trị ở
cả hai `.env`.

Xác thực (backend đặt `GATEWAY_TOKEN`): `Register` bằng khoá tham gia — `WORKER_JOIN_KEY` rồi
`GATEWAY_TOKEN` — nhận `worker_token` riêng, các lời gọi sau (Lease / Complete / Heartbeat / Leave,
Register lại) dùng token đó. Gateway từ chối token (hết hạn sau 7 ngày không chạy, mất Redis) thì
worker bỏ token và vào lại bằng khoá tham gia; khoá một lần đã dùng thì cần khoá mới.

## Healthcheck

Worker ghi file JSON nhỏ `WORKER_HEALTH_FILE` (ghi file tạm rồi đổi tên, không bao giờ đọc phải
file dở) sau mỗi lời gọi gateway thành công (`Register` / `Lease` / `Heartbeat` / `Complete`), khi
lời gọi gateway lỗi, và ở mỗi nhịp heartbeat kể cả lúc mất gateway; trạng thái không đổi thì tối đa
một lần mỗi giây. Nội dung: `worker_id`, `pid`, `state`, `updated_at`, `last_ok_at`,
`disconnected_since`, `heartbeat_seconds` (giờ Unix, giây). Worker dừng hẳn thì xoá file.

| `state` | Nghĩa |
| --- | --- |
| `starting` | Chưa `Register` được lần nào (gateway chưa lên, sai địa chỉ, sai token...) |
| `running` | Lời gọi gateway gần nhất thành công |
| `reconnecting` | Đã từng kết nối, lời gọi gần nhất lỗi; worker đang thử lại |
| `draining` | Thôi xin việc, làm nốt việc đang chạy rồi thoát (SIGTERM hoặc drain từ gateway) |

`jobworker health` trả mã 0 (in `healthy: …`) hoặc 1 (in `unhealthy: <lý do>`) khi:

- không có file (worker chưa chạy / đã dừng) hoặc file hỏng;
- file cũ hơn `--max-age` giây — mặc định `max(30, 3 × heartbeat_seconds)`, theo chu kỳ heartbeat
  gateway đang đặt: event loop treo hoặc process chết;
- `starting` / `reconnecting` liên tục lâu hơn `--gateway-grace` giây (mặc định 300; `0` = bỏ qua).

Mất gateway **không** làm worker không khoẻ ngay: worker tự thử lại mãi, gateway khởi động lại
(vài giây tới vài chục giây) không được kéo theo khởi động lại mọi worker. Quá 5 phút mới báo
không khoẻ (orchestrator khởi động lại container unhealthy lúc đó cũng không hại gì, và gỡ được lỗi
nằm ở phía worker như DNS, kênh gRPC kẹt); sai token cũng hiện thành unhealthy thay vì chỉ có trong
log. `draining` không xét gateway (worker sắp thoát; treo khi dừng thì file cũ).

```yaml
healthcheck:
  test: ["CMD", "jobworker", "health"]
  interval: 30s
  timeout: 10s
  start_period: 30s
  retries: 3
```

Docker (không Swarm) chỉ đánh dấu container `unhealthy`, không tự khởi động lại; worker treo thì
watchdog (bên dưới) cho process thoát để `restart: unless-stopped` chạy lại. Chạy nhiều worker trên
cùng một máy không qua container thì đặt mỗi process một `WORKER_HEALTH_FILE` (và `jobworker health
--file …`).

### Watchdog

Thread trong process (`jobworker.watchdog`) cho worker treo tự thoát với mã **70**, để chính sách
khởi động lại của Docker / compose (`restart: unless-stopped`) hoặc systemd (`Restart=on-failure`)
chạy lại. Hạn `WORKER_WATCHDOG_SECONDS` (mặc định 120; `0` = tắt;
khác 0 thì ít nhất 10).

| `kind` | Thoát khi |
| --- | --- |
| `event_loop` | Event loop không chạy quá hạn (một task ghi `time.monotonic()` mỗi giây, thread kiểm tra mỗi min(5 s, hạn / 4)): lời gọi đồng bộ chặn loop, deadlock trong thư viện C... |
| `main_loop` | Vòng heartbeat (chỗ ghi lại file trạng thái) không qua lượt nào quá hạn + thời gian lượt đó được phép (chờ tới nhịp kế + một lời gọi `Heartbeat`, hạn 15 s): vòng kẹt dù loop vẫn chạy |
| `drain` | Đang dừng (SIGTERM / drain từ gateway) quá `WORKER_SHUTDOWN_GRACE_SECONDS` + 35 s + hạn |

Mất gateway (chưa lên, khởi động lại, mạng nuốt gói, sai token) **không** làm thoát: vòng heartbeat
vẫn quay (lời gọi lỗi ngay hoặc hết hạn), lease thử lại theo backoff tới 30 s không bị xét; mất lâu
thì chỉ `jobworker health` báo `unhealthy` như trên. Khi bắt đầu dừng, watchdog thôi xét vòng
heartbeat (vòng này tắt trước khi worker thoát) và chỉ giữ hạn tổng `drain`, nên dừng êm không bị
giết; dừng xong thì watchdog tắt hẳn.

Khi thoát: log `watchdog.stall` mức critical (`kind`, `idle_seconds`, `limit_seconds`), in stack mọi
thread (faulthandler) ra stderr để biết treo ở đâu, rồi `os._exit(70)` — không chạy dọn dẹp vì loop
đang treo. Cả process bị đóng băng (máy ngủ, `docker pause`, debugger) không tính là treo.

## Secret

Biến nào cũng đọc được từ file trong thư mục secret: mỗi file một biến, tên file là tên biến (không
phân biệt hoa thường), nội dung bỏ khoảng trắng / xuống dòng ở hai đầu. Thư mục là `SECRETS_DIR`
(môi trường hoặc `.env`); không đặt thì `/run/secrets` nếu có — chỗ Docker / compose `secrets:`
mount vào — không có thì không đọc (máy dev không bị ảnh hưởng). Môi trường và `.env` thắng file
secret, nên để trống biến tương ứng.

| File | Biến | Nội dung |
| --- | --- | --- |
| `gateway_token` | `GATEWAY_TOKEN` | Token gateway |
| `worker_proxies` | `WORKER_PROXIES` | Proxy tại worker, định dạng như `config/proxies.example.txt` (mỗi dòng một proxy, `#` là chú thích) hoặc JSON list |

```yaml
services:
  worker:
    secrets: [gateway_token, worker_proxies]   # → /run/secrets/gateway_token, ...
secrets:
  gateway_token:
    file: ./secrets/gateway_token
  worker_proxies:
    file: ./secrets/worker_proxies
```

Cách khác cho proxy: mount file proxy làm secret rồi trỏ `WORKER_PROXIES_FILE` vào nó
(`/run/secrets/worker_proxies`). Proxy từ cả hai nguồn được gộp, bỏ trùng.

## Proxy

Thứ tự chọn proxy cho mỗi việc:

1. Việc có `proxy` (backend `PROXY_MODE=central`: backend chọn từ danh sách chung, gắn theo domain,
   tự chấm sức khoẻ) → dùng đúng proxy đó, `proxy_id` do backend đặt.
2. Việc có `direct` (backend `PROXY_MODE=off`) → đi thẳng, bỏ qua proxy tại worker.
3. Gateway cho worker tự xoay (`PROXY_MODE=worker`, mặc định) và worker có proxy còn dùng được →
   proxy gắn theo domain (sticky `WORKER_PROXY_STICKY_SECONDS`), chọn lần lượt trong các proxy
   còn tốt. Danh sách: proxy riêng của máy (`WORKER_PROXIES` / `WORKER_PROXIES_FILE`, `proxy_id`
   dạng `w:<hash>`) nếu có; không thì **danh sách proxy chung** gateway gửi khi đăng ký
   (`WorkerConfig.shared_proxies`, bảng proxy ở trang Cài đặt; `proxy_id` đúng id của backend). Danh
   sách chung đổi → phiên bản cấu hình đổi → worker đăng ký lại và thay danh sách, giữ sức khoẻ của
   proxy còn lại. Mỗi worker bắt đầu vòng xoay ở một vị trí riêng (theo `worker_id`).
4. Không thì đi thẳng.

Proxy tại worker: lỗi kết nối qua proxy, 403, 407, 429 → nghỉ với domain đó
`WORKER_PROXY_COOLDOWN_SECONDS`; 5 lỗi kết nối liên tiếp → loại 10 phút. Mọi proxy đang nghỉ với
một domain thì việc của domain đó đi thẳng. Chromium không dùng được SOCKS có mật khẩu: các proxy
đó chỉ dùng cho HTTP. Log chỉ in proxy đã che mật khẩu.

Worker không đọc `HTTP(S)_PROXY` của môi trường và không giữ cookie giữa các việc (cookie cần thiết
do backend gửi trong header).

## Pool

`WORKER_POOL` gom worker theo nhóm (vd. `residential` cho worker có proxy dân dụng). Backend định
tuyến domain → pool bằng `FETCH_ROUTES` (`{"topcv.vn": "residential"}`, khớp cả subdomain); pool
không có worker nào sống thì việc đi pool `default`. Mỗi pool cần ít nhất một worker có render
nếu domain trong pool cần Chromium.

## Quy mô

- Slot HTTP rẻ: mỗi request chỉ là một handle curl, 32–64 slot mỗi worker là bình thường; giới hạn
  thực tế là băng thông và số proxy (rate limit theo domain do backend lo).
- Slot render đắt: mỗi trang Chromium chiếm khoảng 250–400 MB RAM và đáng kể CPU. Để trống
  `WORKER_RENDER_SLOTS` thì worker tự tính 1 slot mỗi 768 MiB RAM (giới hạn cgroup nếu chạy trong
  container), không vượt số core; tự đặt khi máy còn chạy việc khác.
- Cần nhiều hơn thì chạy thêm worker (mỗi process một `WORKER_ID`) thay vì tăng slot vô hạn.
  Worker `WORKER_RENDER_SLOTS=0` không mở Chromium, hợp với máy nhỏ chỉ làm HTTP.

## Bảo mật

- Gateway mở ra ngoài mạng nội bộ thì luôn bật xác thực (`GATEWAY_TOKEN` ở backend); máy worker
  mới nên dùng khoá tham gia một lần thay vì chép `GATEWAY_TOKEN`.
  Production: đưa token (và proxy có mật khẩu) qua Docker secrets thay vì biến môi trường (xem
  Secret) để không lộ trong `docker inspect`.
- Mã hoá: `GATEWAY_TLS=true` (gateway có `GATEWAY_TLS_CERT`/`GATEWAY_TLS_KEY`, hoặc đứng sau reverse
  proxy HTTP/2 có TLS). Chứng chỉ tự ký: đưa CA cho worker qua `GATEWAY_CA_CERT`.
- Token và mật khẩu proxy không bao giờ được ghi log: mọi dòng log (kể cả traceback, log của
  grpc / asyncio) che mật khẩu trong URL (`http://user:***@host`), token `Bearer`, tham số bí
  mật trong query string và giá trị của khoá như `password`, `token`, `authorization`, `cookie`.

## Phát triển

```bash
task check                  # ruff format --check + ruff check + pyright + pytest, như CI
uv run pytest -q            # test Chromium tự bỏ qua khi chưa cài (`playwright install chromium`)
task fmt                    # ruff format + ruff check --fix
```

Test dùng gateway giả trong process (`tests/worker_fakes.py`), web server và forward proxy HTTP
chạy ở thread. `tests/` không có `__init__.py`: pytest tự thêm thư mục đó vào `sys.path` để test
import được `worker_fakes`.
