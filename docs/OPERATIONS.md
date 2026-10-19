# Vận hành

Ghi chú để hệ thống tự chạy ổn định: dịch vụ nào chạy gì, giới hạn đang đặt, cách chịu dữ liệu
lớn, và những việc cần làm khi đổi cấu hình. Giao diện web chỉ hiện **trạng thái** và cho sửa cài
đặt; mọi lưu ý vận hành nằm ở đây. Triển khai nhiều máy và quy mô 10.000 doanh nghiệp:
[DEPLOY.md](DEPLOY.md).

## Dịch vụ (docker compose, một máy)

| Dịch vụ         | Việc                                                                              | Số bản                   |
| --------------- | --------------------------------------------------------------------------------- | ------------------------ |
| `postgres`      | dữ liệu (`shm_size: 512mb` — xem [Postgres](#postgres))                           | 1                        |
| `redis`         | hàng đợi (Redis Streams), việc lấy trang của worker, cache, khoá, số đo, cài đặt  | 1                        |
| `migrate`       | `jobcrawler db upgrade` mỗi lần `up` (không có việc thì thoát)                    | —                        |
| `api`           | API + giao diện React, `127.0.0.1:${API_PORT:-8000}`                              | 1                        |
| `gateway`       | gRPC cho worker, `${GATEWAY_PUBLISH:-127.0.0.1:50051}`                            | 1                        |
| `scheduler`     | xếp build đêm / poll ngày mỗi giây (một bản giữ khoá làm việc)                    | 1                        |
| `runner-poll`   | `poll_company` (50 lượt song song mỗi bản)                                        | `RUNNER_POLL_REPLICAS`   |
| `runner-build`  | `build_company` (10 mỗi bản), cron bảo trì, cron số liệu tổng quan                | `RUNNER_BUILD_REPLICAS`  |
| `runner-boards` | index trang tuyển dụng: đủ (00:30), đuôi (mỗi 2 phút, 6h–23h)                     | `RUNNER_BOARDS_REPLICAS` |
| `worker`        | lấy trang (curl_cffi + Chromium) qua proxy của worker                             | `WORKER_REPLICAS`        |

Đổi mã nguồn: `docker compose build && docker compose up -d` (hoặc `task up`; migration chạy tự
động). Image backend (`backend/Dockerfile`) không có Chromium; image worker (`worker/Dockerfile`)
có. Cả hai build từ thư mục gốc repo và luôn cài lại gói của workspace (cache của uv cho gói cục
bộ chỉ theo `pyproject.toml`, không ép thì image có thể mang mã cũ).

- Postgres (`15432`) và Redis (`16379`) chỉ mở ở `127.0.0.1` của máy host (Redis không có mật
  khẩu). Chạy CLI / test trên máy host thì dùng `127.0.0.1` trong URL, không dùng `localhost`:
  trên Windows `localhost` thử IPv6 `::1` trước và mỗi kết nối mới chờ thêm ~2 s.
- Log của mọi container xoay vòng (json-file, 10 MB × 3).
- `gateway` có healthcheck (`jobcrawler gateway-health`, dịch vụ health chuẩn của gRPC, dùng TLS
  khi đặt `GATEWAY_TLS_CERT/KEY`); `worker` chỉ khởi động khi gateway healthy. Image backend cũ
  (trước lệnh này) làm gateway luôn unhealthy: build lại image trước khi `up`.

## Lịch thu thập

- **Doanh nghiệp mới** (vừa `import` / nạp ở giao diện): build ngay, bất kể giờ.
- **Build đêm** trong `BUILD_WINDOW` (00:00–06:00 theo `CRAWL_TIMEZONE`): mỗi doanh nghiệp một lần,
  rải đều theo pha cố định; tìm nguồn lại từ đầu (`BUILD_REFRESH_DISCOVERY`).
- **Poll ngày** trong `POLL_WINDOW` (06:00–24:00) mỗi `POLL_INTERVAL_SECONDS` (150 s) ±
  `POLL_JITTER_SECONDS`. Poll lỗi → build lại ngay; build lại vẫn hỏng → thử lại sau 2 giờ, doanh
  nghiệp ở trạng thái "Bộ khung hỏng" (tab "Bộ khung & lịch" ghi lý do).
- **Trang tuyển dụng có tin mới** → poll ngay doanh nghiệp có profile đó (không chờ chu kỳ).
- Ngoài khung poll không có poll; tin đăng giữa đêm được lượt build đêm thấy.

Đổi khung giờ / chu kỳ ở trang Cài đặt có hiệu lực trong vài giây (scheduler đọc lại mỗi nhịp).
Cron (`BOARD_INDEX_CRON`, `BOARD_TAIL_CRON`, `MAINTENANCE_CRON`) đọc theo `CRAWL_TIMEZONE` và cần
khởi động lại runner tương ứng.

## Độ trễ phát hiện tin mới

Trang Tổng quan → "Độ trễ phát hiện tin mới" (API `/api/crawl/status`):

- **Cửa sổ phát hiện** của mỗi lượt poll đủ = lúc xong − lúc bắt đầu lượt poll đủ trước: tin đăng
  ngay sau lượt trước bắt đầu sẽ được thấy muộn nhất lúc này. P95 phải ≤ 180 s.
- **Độ tươi** = tuổi lượt poll đủ gần nhất của từng doanh nghiệp đang poll (ảnh chụp lúc này).
- P95 vượt mục tiêu thường do: hàng đợi poll dồn (thêm `runner-poll` hoặc worker), lượt poll dài
  (site phân trang dài → `POLL_MAX_LISTING_PAGES`, rate limit theo domain), doanh nghiệp đang build
  (poll bị bỏ qua trong lúc build), worker thiếu (trang Workers: slot đang dùng sát tổng).

Đo đầu-cuối trên site giả (không phụ thuộc site thật có đăng tin):

```bash
uv run python scripts/loadtest/sla_site.py --port 8765 --every 75 --count 16 --out sla.jsonl
# nạp doanh nghiệp website http://host.docker.internal:8765/ (từ container), chờ build xong
uv run python scripts/loadtest/sla_report.py sla.jsonl
```

## Bộ khung và poll — khi nào đóng việc

- Poll chỉ đóng việc khi nguồn **đọc trọn** (mọi trang danh sách / API trong bộ khung, không lỗi,
  không bị cắt) và tin vắng mặt không phải tin "gián tiếp" (chỉ thấy qua trang tin khác hoặc qua
  feed không phân trang kiểu "việc làm nổi bật", có thể xoay vòng).
- **Mất tin hàng loạt** (≥ 30% tin, ≥ 3 tin, nguồn ≥ 5 tin) trong một lượt: không đóng, đếm như lỗi;
  lặp 3 lượt (`POLL_MAX_FAILURES`) thì build lại — lượt build đọc đủ mới đóng đúng việc đã gỡ.
- `POLL_MAX_LISTING_PAGES` > 0: poll chỉ đọc N trang danh sách đầu (rẻ hơn với site phân trang dài,
  hợp với site xếp tin mới lên đầu); khi đó tin đã gỡ chỉ được đóng ở lượt build đêm.
- `POLL_MAX_DETAIL_PAGES` (10): số trang tin mới mở tối đa mỗi nguồn mỗi lượt poll; phần còn lại mở
  ở lượt sau (lượt bị cắt không đóng việc).
- Poll không cập nhật `last_seen_at` của tin còn thấy (tránh ghi hàng chục dòng mỗi lượt); lượt
  build đêm cập nhật. Độ tươi của doanh nghiệp xem ở `company_schedules.last_poll_ok_at`.

## Worker lấy trang

- Trang **Workers**: worker đang sống, pool, slot HTTP / render đang dùng, proxy tại worker, việc
  xong / lỗi, CPU / RAM; "Dừng nhận việc" để worker làm nốt rồi thoát (với restart policy, nó lên
  lại với id mới — dùng khi thay proxy / nâng cấp).
- Không có worker nào → mọi request lỗi `no_worker` ngay (lượt chạy ghi lỗi, poll đếm lỗi); có
  worker trở lại thì tự chạy tiếp.
- Cấu hình đẩy xuống mọi worker (slot, impersonate, heartbeat…) sửa ở trang Cài đặt → nhóm
  "Worker lấy trang"; worker nhận trong ≤ 5 s. Slot đặt ở đây là **mức trần**: worker dùng số nhỏ
  hơn giữa giá trị này và `WORKER_HTTP_SLOTS` / `WORKER_RENDER_SLOTS` của máy nó.
- Chromium lỗi (không mở được, crash): worker tạm tắt render rồi thử lại sau 30 s, 1, 2… tối đa 10
  phút; trang Workers ghi "Tạm tắt · thử lại sau …". Máy không cài Chromium thì tắt hẳn.
- Cột "Xong / lỗi" của trang Workers kèm "trả lại" (việc giao lại khi worker rời) và "bỏ" (kết
  quả gửi muộn): tăng đều là dấu hiệu worker mất kết nối / quá tải.
- Mở gateway ra ngoài mạng nội bộ: đặt `GATEWAY_TOKEN` (worker gửi cùng token) và TLS
  (`GATEWAY_TLS_CERT/KEY`, hoặc đặt sau reverse proxy có TLS hỗ trợ HTTP/2).

## Cài đặt sửa ở giao diện

Trang Cài đặt ghi đè giá trị từ `.env` (bảng `app_settings`). Mỗi lần lưu tăng phiên bản trên Redis;
mọi process (api, gateway, scheduler, runner) nạp lại trong ≤ 5 s. Mục ghi "Cần khởi động lại" chỉ
có hiệu lực khi khởi động lại process dùng nó (endpoint LLM, cấu hình HTTP, proxy chung, cron, số
job song song của runner). "Khôi phục" xoá giá trị ghi đè, về giá trị trong `.env` / mặc định. Các
mục kết nối (DB, Redis, cổng, token, mật khẩu giao diện) chỉ đặt bằng `.env`.

## Index trang tuyển dụng

- **Đủ** (đêm): đọc toàn bộ danh sách, tải tin mới / đổi / lâu chưa tải, đánh dấu tin gỡ, map
  profile. Giới hạn `BOARD_MAX_JOBS_PER_RUN`, `BOARD_INDEX_BUDGET_SECONDS`, `BOARD_CONCURRENCY`.
- **Đuôi** (ngày, mỗi 2 phút): 1–4 request mỗi trang tới phần mới nhất, chỉ tải tin chưa từng
  thấy. Độ tươi bị giới hạn bởi tần suất trang tự làm mới danh sách (sitemap TopCV / JobsGO ~1 giờ;
  CareerViet, ITviec, VietnamWorks, CareerLink, Vieclam24h, 123job gần như tức thời).
- Chưa có proxy cho trang tuyển dụng: TopCV chặn bằng Cloudflare khi đi thẳng, CareerLink hiện
  CAPTCHA sau vài chục request. Dùng pool worker riêng có proxy dân cư cho các domain này
  (`FETCH_ROUTES={"topcv.vn": "residential", "careerlink.vn": "residential"}`).
- Sau khi đổi luật so tên: `uv run jobcrawler index-boards --rematch`.

## Hàng đợi (Redis Streams)

- Ba queue: `jobcrawler-build`, `jobcrawler-poll`, `jobcrawler-boards` (stream `sq:<queue>:stream`,
  consumer group `workers`, mỗi runner một consumer, chỉ đọc khi còn slot rảnh). Trang Hàng đợi hiện
  đang chạy / chờ / hẹn giờ, lý do xếp (đêm / chu kỳ / bộ khung hỏng / trang tuyển dụng / thủ công).
- Khoá job theo doanh nghiệp: một doanh nghiệp chỉ có một build và một poll đang chờ / chạy; build
  và poll của cùng doanh nghiệp không chạy chồng (khoá Redis, poll gặp build thì bỏ qua lượt đó).
- **Runner chết** (OOM, kill): runner sống báo mỗi 10 s; entry im quá `QUEUE_RECLAIM_SECONDS` (60 s)
  bị runner khác thu hồi và chạy lại. **Dừng êm** (deploy): runner ngừng nhận việc, chờ 2 s, huỷ
  job đang chạy và xếp lại ngay (không tính vào số lần chạy).
- Việc lấy trang (`<prefix>:gw:fetch:<pool>:http|render`, việc của lượt poll đi làn `…:hi` và
  được worker lấy trước) là hàng "nhiều nhất một lần". Gateway ghi worker nào đang giữ việc nào
  (`<prefix>:gw:inflight:<worker>`): worker rời / quá hạn báo sống thì việc nó giữ được trả về
  backend ngay (`worker_lost`) để giao lại; kết quả gửi muộn sau đó bị bỏ. Hàng đông mà worker
  vẫn đang lấy việc thì backend chờ tiếp từng khung `timeout + FETCH_QUEUE_GRACE_SECONDS`, tổng
  tối đa `timeout + FETCH_QUEUE_MAX_WAIT_SECONDS` (300 s); không ai lấy việc thì thôi chờ và
  `HttpClient` tự thử lại.
- Rủi ro còn lại: runner treo (không báo sống) quá 60 s rồi tỉnh lại → một job chạy hai lần (ghi
  kết quả vẫn đúng nhờ gộp theo dấu vân tay); Redis mất dữ liệu (AOF cụt) → mất vài job vừa xếp
  (scheduler tự xếp lại poll / build đến hạn ở nhịp sau).

## Chịu dữ liệu lớn

Mục tiêu: mọi thao tác trên giao diện dưới ~300 ms khi có hàng triệu việc làm.

- **ID dạng UUIDv7**: khoá chính mọi bảng là UUID gồm 48 bit thời gian + phần ngẫu nhiên, sinh ở
  ứng dụng (`db/ids.py`) hoặc bằng `uuid_v7()` của DB khi insert SQL thô. Riêng
  `compression_dicts.id` là số (mã từ điển ghi trong từng giá trị nén).
- **Không trang nào tải cả bảng.** Mọi danh sách phân trang ở server; đếm có giới hạn (10.000+).
- **Index theo đúng truy vấn**: mỗi kiểu sắp xếp việc làm có index `(status, khoá sắp xếp, id)`;
  GIN trigram cho tìm tên; BRIN theo thời gian; `company_schedules` index một phần cho
  `next_poll_at` của doanh nghiệp đang poll.
- **Lịch cập nhật nhiều**: `company_schedules` cập nhật mỗi lượt poll (10.000 doanh nghiệp ≈ 67
  dòng/giây) nên tách khỏi `companies`, fillfactor 70 (cập nhật HOT) và autovacuum dày hơn.
- **Lượt poll không đổi gì không ghi `company_runs` / `source_fetches`** — chỉ lượt có tin mới /
  tin gỡ / lỗi mới ghi; số đo poll nằm ở Redis (histogram theo phút, giữ 3 ngày).
- **Tổng quan không quét lại mỗi lần mở**: phân bố do cron tính mỗi 5 phút, lưu Redis.

Đo truy vấn trên dữ liệu giả lập 100 nghìn doanh nghiệp / 1 triệu việc làm:
`uv run python scripts/loadtest/seed.py` rồi `uv run python scripts/loadtest/bench.py --prepare`.

## Không có việc làm trùng

- **Trong một lượt**, các tin cùng việc được gộp (`pipeline/dedup.py`): cùng tiêu đề, cùng nơi làm,
  JD giống nhau; tin cùng nguồn đăng lại với mã mới → một việc.
- **Với việc đã lưu** (cả lượt poll chỉ xử lý tin mới), tin mới được gắn vào việc đang mở cùng dấu
  vân tay nội dung hoặc khớp mờ cùng luật trên.
- **Ở tầng DB**, index duy nhất `ux_jobs_company_fingerprint_active` chặn hai việc đang mở y hệt.

## Truy vết một thao tác

- Mỗi request API có `X-Request-Id` (client gửi UUID thì dùng lại, không thì sinh mới); mọi log
  của request kèm `request_id`, lỗi 500 trả `request_id` cho người dùng báo lại. Tìm log: bảng
  `logs`, cột `context->>'request_id'`.
- Trang **Nhật ký thao tác** (bảng `audit_events`): ai lưu cài đặt nào (giá trị trước → sau, bí
  mật bị che), ai bấm build / poll / import / kiểm tay / bảo trì / dừng worker, kèm `request_id`.
  Bảng chỉ ghi thêm và **không** bị bảo trì dọn; muốn giới hạn dung lượng thì xoá tay theo
  `occurred_at`.

## Đăng nhập giao diện

Đặt `UI_PASSWORD` (HTTP Basic). Một IP sai mật khẩu 20 lần (mỗi lần cách nhau dưới 5 phút) thì
nhận 429 tới khi hết 5 phút, kể cả khi nhập đúng; đăng nhập đúng xoá bộ đếm. Bộ đếm ở Redis;
Redis lỗi thì không chặn ai. Sau reverse proxy, uvicorn chỉ thấy IP thật khi IP của proxy nằm
trong `FORWARDED_ALLOW_IPS` (mặc định `127.0.0.1`); không thì mọi người dùng chung một bộ đếm của
IP proxy.

## Bảo trì

Tự động mỗi giờ (`MAINTENANCE_CRON`): lượt chạy treo → `failed`, đối chiếu cột tóm tắt doanh
nghiệp, gộp việc trùng còn sót, xoá dữ liệu cũ (`failed_pages` 7 ngày, `source_fetches` / `logs`
30 ngày; `audit_events` giữ mãi), huấn luyện từ điển nén và nén lại dần.

```bash
uv run jobcrawler db maintain      # như cron
uv run jobcrawler db reclean       # áp lại bộ làm sạch nội dung lên việc đã lưu (sau khi đổi luật)
uv run jobcrawler db dedup         # điền dấu vân tay + gộp việc trùng
```

## Postgres

`/dev/shm` mặc định của container (64 MB) không đủ cho truy vấn / VACUUM song song trên bảng
lớn, nên compose đặt `shm_size: 512mb`. Schema dựng lại từ đầu ngày 19/10/2026
(`0001_initial_schema`), sau đó `0002_audit_events`; migration mới sinh bằng
`task migration NAME="…"` (`uv run jobcrawler db revision -m "…"`), không sửa revision đã chạy.

## Sự cố hay gặp

- **Docker Desktop tắt** (máy ngủ / cập nhật): mọi container dừng; mở lại Docker Desktop, các
  container có `restart: unless-stopped` tự lên. Worker tự nối lại gateway (lùi tới 30 s).
- **LLM trả lỗi** (vd. 503 "Gateway control plane unavailable"): mỗi lời gọi tốn ~4 s (2 lần thử)
  rồi bỏ; KYC ra `not_found` / `ambiguous`, tin cần LLM xác nhận bị hoãn (lượt `partial`, lượt sau
  chấm lại). `uv run jobcrawler llm check` để kiểm tra.
- **Test tích hợp chập chờn trên Windows** (`WinError 64` khi nối Redis): chạy lại từng file test.
  Các test tích hợp dùng chung DB `jobcrawler_test` (schema bị dựng lại; tên DB bắt buộc kết thúc
  bằng `_test`): không chạy hai bộ test tích hợp song song.
- **Redis không lên sau khi máy hết bộ nhớ / tắt đột ngột** (log Redis: "Bad file format reading
  the append only file …incr.aof"): lệnh ghi cuối của AOF bị cắt dở. Dừng Redis, sao lưu rồi cắt
  phần hỏng, sau đó bật lại cả stack:

  ```bash
  docker compose stop redis
  docker compose run --rm --no-deps -T --entrypoint sh redis -c \
    'b=/data/aof-backup-$(date +%Y%m%d-%H%M%S); mkdir -p $b && cp -a /data/appendonlydir/. $b/ &&
     cd /data/appendonlydir && echo y | redis-check-aof --fix appendonly.aof.manifest'
  docker compose up -d
  ```
