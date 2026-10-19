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

```bash
# Trên máy dev (từ thư mục gốc repo, venv chung của workspace)
GATEWAY_ADDR=localhost:50051 uv run jobworker run
uv run jobworker check      # kết nối gateway + IP đi ra qua từng proxy tại worker
uv run jobworker version

# Docker (build từ thư mục gốc repo)
docker build -f worker/Dockerfile -t jobcrawler-worker .
docker run --init --env-file worker.env --stop-timeout 40 jobcrawler-worker
```

Compose: đặt `init: true` (dọn process con của Chromium) và `stop_grace_period` lớn hơn
`WORKER_SHUTDOWN_GRACE_SECONDS` vài giây.

Dừng (SIGTERM / Ctrl-C): thôi xin việc, chờ việc đang chạy tối đa `WORKER_SHUTDOWN_GRACE_SECONDS`
(kết quả vẫn được gửi), `Leave` rồi thoát. Tín hiệu lần hai: huỷ luôn việc đang chạy (backend nhận
lỗi và thử lại ngay). Gateway yêu cầu drain (trang Workers) thì worker làm nốt việc rồi thoát với
mã 0.

## Biến môi trường

Đọc từ môi trường và file `.env` ở thư mục chạy. Biến trống coi như không đặt.

| Biến | Mặc định | Ý nghĩa |
| --- | --- | --- |
| `GATEWAY_ADDR` | `localhost:50051` | Địa chỉ gateway gRPC (`host:port`) |
| `GATEWAY_TOKEN` | — | Token gửi kèm mỗi lời gọi (`authorization: Bearer …`), khớp `GATEWAY_TOKEN` của backend |
| `GATEWAY_TLS` | `false` | Kết nối TLS |
| `GATEWAY_CA_CERT` | — | File PEM của CA riêng (chứng chỉ tự ký); trống = CA hệ thống |
| `WORKER_ID` | `<hostname>-<8 hex>` | Id duy nhất mỗi process; mặc định sinh mới mỗi lần khởi động |
| `WORKER_POOL` | `default` | Nhóm worker (xem Pool bên dưới) |
| `WORKER_HTTP_SLOTS` | `32` | Request HTTP cùng lúc tối đa |
| `WORKER_RENDER_SLOTS` | `2` | Trang Chromium cùng lúc tối đa; `0` = không bao giờ khởi động Chromium |
| `WORKER_PROXIES` | — | Proxy tại worker: JSON list hoặc cách nhau bởi dấu phẩy / xuống dòng |
| `WORKER_PROXIES_FILE` | — | File proxy, định dạng như `config/proxies.example.txt` |
| `WORKER_IMPERSONATE` | `chrome` | Fingerprint curl_cffi |
| `WORKER_LABELS` | `{}` | Nhãn tự do, JSON (`{"region": "hn", "provider": "x"}`), hiện ở trang Workers |
| `WORKER_MAX_BODY_BYTES` | `10485760` | Body lớn hơn thì trả `ERROR_KIND_TOO_LARGE` (không kèm body) |
| `WORKER_SHUTDOWN_GRACE_SECONDS` | `30` | Chờ việc đang chạy khi dừng |
| `WORKER_PROXY_STICKY_SECONDS` | `600` | Giữ một proxy cho mỗi domain chừng này giây |
| `WORKER_PROXY_COOLDOWN_SECONDS` | `120` | Proxy nghỉ với domain sau lỗi kết nối / 403 / 407 / 429 |
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

Cấu hình đẩy xuống từ backend nằm ở các biến `GATEWAY_HTTP_SLOTS`, `GATEWAY_RENDER_SLOTS`,
`GATEWAY_IMPERSONATE`… của backend (hoặc trang Cài đặt), không trùng tên với biến `WORKER_*` ở đây,
nên worker và backend đọc chung một `.env` được.

## Proxy

Thứ tự chọn proxy cho mỗi việc:

1. Việc có `proxy` (backend `PROXY_MODE=central`: backend chọn từ danh sách chung, gắn theo domain,
   tự chấm sức khoẻ) → dùng đúng proxy đó, `proxy_id` do backend đặt.
2. Việc có `direct` (backend `PROXY_MODE=off`) → đi thẳng, bỏ qua proxy tại worker.
3. Gateway cho dùng proxy tại worker (`PROXY_MODE=worker`) và worker có proxy còn dùng được →
   proxy gắn theo domain (sticky `WORKER_PROXY_STICKY_SECONDS`), chọn lần lượt trong các proxy
   còn tốt; `proxy_id` dạng `w:<hash>`.
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
- Slot render đắt: mỗi trang Chromium chiếm khoảng 250–400 MB RAM và đáng kể CPU. Tính
  `WORKER_RENDER_SLOTS ≈ (RAM trống − 500 MB) / 400 MB`, và không vượt số core.
- Cần nhiều hơn thì chạy thêm worker (mỗi process một `WORKER_ID`) thay vì tăng slot vô hạn.
  Worker `WORKER_RENDER_SLOTS=0` không mở Chromium, hợp với máy nhỏ chỉ làm HTTP.

## Bảo mật

- Gateway mở ra ngoài mạng nội bộ thì luôn đặt `GATEWAY_TOKEN` (cùng giá trị ở backend và worker).
- Mã hoá: `GATEWAY_TLS=true` (gateway có `GATEWAY_TLS_CERT`/`GATEWAY_TLS_KEY`, hoặc đứng sau reverse
  proxy HTTP/2 có TLS). Chứng chỉ tự ký: đưa CA cho worker qua `GATEWAY_CA_CERT`.
- Token và mật khẩu proxy không bao giờ được ghi log: mọi dòng log (kể cả traceback, log của
  grpc / asyncio) che mật khẩu trong URL (`http://user:***@host`), token `Bearer`, tham số bí
  mật trong query string và giá trị của khoá như `password`, `token`, `authorization`, `cookie`.

## Phát triển

```bash
uv run pytest worker/tests -q          # test Chromium tự bỏ qua khi chưa cài Chromium
uv run ruff check worker && uv run pyright worker/
```

Test dùng gateway giả trong process (`worker/tests/worker_fakes.py`), web server và forward proxy
HTTP chạy ở thread. `worker/tests` không có `__init__.py` (backend đã có package `tests`).
