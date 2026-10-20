# Vận hành

Ghi chú để hệ thống tự chạy ổn định: dịch vụ nào chạy gì, giới hạn đang đặt, cách chịu dữ liệu
lớn, và những việc cần làm khi đổi cấu hình. Giao diện web chỉ hiện **trạng thái** và cho sửa cài
đặt; mọi lưu ý vận hành nằm ở đây. Triển khai nhiều máy và quy mô 10.000 doanh nghiệp:
[DEPLOY.md](DEPLOY.md).

## Dịch vụ (docker compose, một máy)

| Container  | Việc                                                                                              | Số bản                |
| ---------- | ------------------------------------------------------------------------------------------------- | --------------------- |
| `postgres` | dữ liệu (`shm_size: 512mb` — xem [Postgres](#postgres))                                           | 1                     |
| `redis`    | hàng đợi (Redis Streams), việc lấy trang của worker, khoá, số đo, cài đặt, cache — AOF, `noeviction` | 1                  |
| `backend`  | `jobcrawler serve`: migration rồi API JSON (chỉ trong mạng compose), gateway gRPC (`${GATEWAY_PUBLISH:-127.0.0.1:50051}`), scheduler, runner (poll 50 lượt song song, build 10 + cron bảo trì / số liệu, index trang tuyển dụng) | 1 |
| `frontend` | giao diện React (nginx) + chuyển `/api/` tới `backend`, `127.0.0.1:${WEB_PORT:-8000}`             | 1                     |
| `worker`   | lấy trang (curl_cffi + Chromium) qua proxy của worker                                             | `WORKER_REPLICAS` (1) |

Mỗi service là một thư mục tự đủ (`backend/`, `worker/`, `frontend/`; `proto/` là thư viện dùng
chung) với `.env` riêng: `backend/.env` cho container `backend`, `worker/.env` cho worker, `.env`
ở gốc chỉ cho biến của compose (cổng, số worker, tài khoản Postgres). Lệnh `jobcrawler …` chạy trong
`backend/` (`cd backend && uv run jobcrawler …` hoặc `task backend:cli -- …`).

Đổi mã nguồn: `docker compose build && docker compose up -d` (hoặc `task up`; migration chạy tự
động). Image backend (`backend/Dockerfile`) không có Chromium; image worker (`worker/Dockerfile`)
có. Cả hai build từ thư mục service (cộng build context `proto`) và luôn cài lại gói cục bộ (cache
của uv cho gói cục bộ chỉ theo `pyproject.toml`, không ép thì image có thể mang mã cũ).

- Postgres (`15432`) và Redis (`16379`) chỉ mở ở `127.0.0.1` của máy host (Redis không có mật
  khẩu). Chạy CLI / test trên máy host thì dùng `127.0.0.1` trong URL, không dùng `localhost`:
  trên Windows `localhost` thử IPv6 `::1` trước và mỗi kết nối mới chờ thêm ~2 s.
- Log của mọi container xoay vòng (json-file, 10 MB × 3).
- `frontend` và `worker` chỉ khởi động khi `backend` healthy (xem
  [Sức khoẻ](#sức-khoẻ-và-tự-khởi-động-lại)).

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
khởi động lại `backend`.

## Độ trễ phát hiện tin mới

Trang Tổng quan → "Độ trễ phát hiện tin mới" (API `/api/crawl/status`):

- **Cửa sổ phát hiện** của mỗi lượt poll đủ = lúc xong − lúc bắt đầu lượt poll đủ trước: tin đăng
  ngay sau lượt trước bắt đầu sẽ được thấy muộn nhất lúc này. P95 phải ≤ 180 s.
- **Độ tươi** = tuổi lượt poll đủ gần nhất của từng doanh nghiệp đang poll (ảnh chụp lúc này).
- P95 vượt mục tiêu thường do: hàng đợi poll dồn (thêm worker, tăng `RUNNER_POLL_CONCURRENCY`), lượt
  poll dài (site phân trang dài → `POLL_MAX_LISTING_PAGES`, rate limit theo domain), doanh nghiệp
  đang build (poll bị bỏ qua trong lúc build), worker thiếu (trang Workers: slot đang dùng sát
  tổng).

Đo đầu-cuối trên site giả (không phụ thuộc site thật có đăng tin):

```bash
cd backend
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

## Proxy

- Hai cách: **Proxy chung** (trang Cài đặt → Proxy → Chế độ "Proxy chung") — danh sách lưu trong
  DB, backend chọn proxy cho từng request và worker dùng đúng proxy đó; **Proxy tại worker** (mặc
  định) — mỗi máy worker dùng proxy của nó (`WORKER_PROXIES` / `WORKER_PROXIES_FILE` trong
  `worker/.env`).
- Bảng proxy chung: thêm / sửa / xoá từng proxy (loại HTTP / HTTPS / SOCKS, host, cổng, tài khoản,
  mật khẩu), "Dán nhiều dòng" (`http://user:pass@host:port`, `host:port`, `host:port:user:pass`;
  trùng bỏ qua) rồi bấm Lưu. Mật khẩu không bao giờ trả lại giao diện; sửa proxy khác host / cổng /
  tài khoản thì phải nhập lại mật khẩu.
- Xoay vòng: mỗi domain gắn một proxy trong "Giữ proxy theo domain" (30 phút), chọn ngẫu nhiên theo
  điểm (0–100: thành công +1, bị chặn −5, lỗi kết nối −10); 403 / 429 thì cho proxy nghỉ với domain
  đó; lỗi kết nối liên tiếp đủ ngưỡng thì loại proxy khỏi vòng xoay rồi thử lại sau "Thời gian loại
  proxy". Bảng hiện trạng thái từng proxy (trong vòng xoay + điểm, bị loại + còn bao lâu, số domain
  đang nghỉ / đang gắn), tải lại mỗi 15 s.
- "Kiểm tra" / "Kiểm tra tất cả" (proxy đã lưu, 20 proxy mỗi lần): worker gọi "Trang kiểm tra
  proxy" qua từng proxy, báo độ trễ, IP ra, quốc gia; proxy sống được đưa lại vào vòng xoay, proxy
  lỗi bị loại. Không có worker thì báo lỗi, không loại proxy.

## Worker lấy trang

- Trang **Workers**: worker đang sống, pool, slot HTTP / render đang dùng, proxy tại worker, việc
  xong / lỗi, CPU / RAM; "Dừng nhận việc" để worker làm nốt rồi thoát (với restart policy, nó lên
  lại với id mới — dùng khi thay proxy / nâng cấp).
- Không có worker nào → mọi request lỗi `no_worker` ngay (lượt chạy ghi lỗi, poll đếm lỗi); có
  worker trở lại thì tự chạy tiếp.
- Cấu hình đẩy xuống mọi worker (slot, impersonate, heartbeat…) sửa ở trang Cài đặt → nhóm
  "Worker lấy trang"; worker nhận trong ≤ 5 s. Slot đặt ở đây là **mức trần**: worker dùng số nhỏ
  hơn giữa giá trị này và `WORKER_HTTP_SLOTS` / `WORKER_RENDER_SLOTS` của máy nó — hai biến này để
  trống thì worker tự tính theo CPU / RAM của máy (giới hạn cgroup nếu trong container): HTTP 8 mỗi
  CPU trong [8, 64], render 1 mỗi 768 MiB RAM trong [1, 8].
- Số job song song của runner (Cài đặt → Runner & hàng đợi) mặc định 0 = tự tính theo máy chạy
  backend (poll 12 mỗi CPU trong [20, 100], build 2,5 mỗi CPU không quá 2 mỗi GiB RAM trong [4, 20],
  trang tuyển dụng 1 mỗi CPU trong [2, 8]); ô cài đặt ghi giá trị máy đang tính ra. Trang Hàng đợi
  hiện "đang chạy / tối đa" của từng hàng đợi: chạm trần mà còn việc chờ thì tăng số hoặc thêm
  runner / worker.
- Chromium lỗi (không mở được, crash): worker tạm tắt render rồi thử lại sau 30 s, 1, 2… tối đa 10
  phút; trang Workers ghi "Tạm tắt · thử lại sau …". Máy không cài Chromium thì tắt hẳn.
- Cột "Xong / lỗi" của trang Workers kèm "trả lại" (việc giao lại khi worker rời) và "bỏ" (kết
  quả gửi muộn): tăng đều là dấu hiệu worker mất kết nối / quá tải.
- Xác thực worker (bật khi đặt `GATEWAY_TOKEN` trong `backend/.env`): worker vào lần đầu bằng một
  khoá tham gia rồi nhận token riêng, dùng cho mọi lời gọi sau (token chỉ nằm trên Redis, hạn trượt
  7 ngày). Khoá tham gia là `GATEWAY_TOKEN` (đặt cùng giá trị ở `worker/.env` — worker trong
  compose, máy worker của mình) hoặc khoá **dùng một lần** tạo ở trang Workers → "Thêm worker" (hết
  hạn sau 5 phút; đặt vào `WORKER_JOIN_KEY` của máy worker mới). Worker lưu token ở
  `WORKER_TOKEN_FILE` (mặc định `~/.jobworker/token`): máy chỉ có khoá một lần thì mount volume vào
  đó, không thì tạo lại container phải xin khoá mới. Token hết hạn / mất Redis: worker tự vào lại
  bằng `GATEWAY_TOKEN` nếu có. Đổi `GATEWAY_TOKEN` không thu hồi token đã cấp (chờ hết hạn, hoặc xoá
  khoá `<prefix>:gw:wtoken:*` trên Redis).
- Mở gateway ra ngoài mạng nội bộ: luôn đặt `GATEWAY_TOKEN` và TLS (`GATEWAY_TLS_CERT/KEY`, hoặc đặt
  sau reverse proxy có TLS hỗ trợ HTTP/2).

## Cài đặt sửa ở giao diện

Cài đặt nằm trong DB (bảng `app_settings`) và sửa ở trang Cài đặt; `backend/.env` chỉ còn những
gì cần để khởi động (kết nối DB / Redis, gateway, đăng nhập giao diện, watchdog, log — xem
`backend/.env.example`). Mỗi lần lưu tăng phiên bản trên Redis; mọi process (api, gateway,
scheduler, runner) nạp lại trong ≤ 5 s.

- Mục có biểu tượng ↻ (endpoint LLM, cấu hình HTTP, proxy chung, cron, số job song song của
  runner…) chỉ đọc lúc process khởi động. Lưu mà đổi chúng thì `jobcrawler serve` (container
  `backend`) tự khởi động lại bốn process con trong vòng vài giây (job đang chạy được xếp lại,
  worker nối lại gateway); trang Cài đặt hiện "Đang áp dụng" tới khi xong. Chạy từng process riêng
  (dev, runner thêm ở máy khác) thì phải tự khởi động lại chúng.
- Một cài đặt vẫn đặt được bằng biến môi trường cùng tên (`LLM_BASE_URL=…` trong `backend/.env`):
  trang Cài đặt ghi "Biến môi trường" và có nút **Lưu vào cơ sở dữ liệu** chép mọi giá trị như vậy
  vào DB (giá trị đang dùng không đổi); sau đó xoá các dòng đó khỏi `.env` được. Giá trị lưu ở
  trang Cài đặt luôn thắng biến môi trường.
- "Khôi phục" xoá giá trị trong DB, về giá trị biến môi trường / mặc định.

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
`uv run python scripts/loadtest/seed.py` rồi `uv run python scripts/loadtest/bench.py --prepare`
(trong `backend/`).

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

- **Trang đăng nhập** (khi đặt `UI_PASSWORD` trong `backend/.env`; tài khoản `UI_USERNAME` /
  `UI_PASSWORD`): phiên là JWT trong cookie httpOnly, hạn `AUTH_TOKEN_HOURS` (12 giờ), ký bằng
  `JWT_SECRET` (trống = khoá tự sinh trên Redis). "Đăng xuất" thu hồi phiên ngay. Đổi mật khẩu /
  `JWT_SECRET` thì mọi phiên cũ hết dùng. Script gọi API: `POST /api/auth/login` lấy
  `access_token`, gửi `Authorization: Bearer <token>`. Một IP sai mật khẩu 20 lần (mỗi lần cách
  nhau dưới 5 phút) thì nhận 429 tới khi hết 5 phút, kể cả khi nhập đúng; đăng nhập đúng xoá bộ
  đếm (Redis; Redis lỗi thì không chặn ai). Để trống `UI_PASSWORD` = không hỏi đăng nhập (chỉ khi
  cổng chỉ mở ở 127.0.0.1). `frontend` (nginx) gửi IP của người kết nối
  tới nó cho API; `frontend` đứng sau một load balancer khác thì đặt `REAL_IP_FROM` (`.env` gốc) bằng CIDR
  của load balancer — nó phải **ghi thêm** IP client vào `X-Forwarded-For` — không thì mọi người
  dùng chung bộ đếm của IP load balancer.
- **SSO** (nhiều người vận hành, mỗi người một tài khoản): đặt một proxy SSO (vd. oauth2-proxy)
  trước `frontend`, gửi `X-Forwarded-User` / `X-Forwarded-Email`; `backend/.env`:
  `AUTH_PROXY_USER_HEADER` (và `AUTH_PROXY_EMAIL_HEADER`, `AUTH_LOGOUT_URL` cho nút "Đăng xuất").
  API lấy người dùng từ header đó, không dùng trang đăng nhập. Chỉ an toàn khi mọi request đi qua
  proxy đó (proxy ghi đè header).

## Sức khoẻ và tự khởi động lại

- Healthcheck: `postgres` (`pg_isready`), `redis` (`PING`), `frontend` (`/healthz` của nginx),
  `worker` (`jobworker health`: file trạng thái trong container; mất liên lạc gateway quá 5 phút
  mới hỏng để backend khởi động lại không kéo theo mọi worker), `backend` gộp: `/healthz` của API,
  `jobcrawler gateway-health` (health chuẩn của gRPC, TLS khi đặt `GATEWAY_TLS_CERT/KEY`) và
  `jobcrawler health scheduler runner` (heartbeat Redis ghi trong vòng lặp chính mỗi 5 s, cũ hơn
  15 s là hỏng).
- Docker chỉ **đánh dấu** container unhealthy, không tự khởi động lại. Vì vậy mỗi process có
  watchdog: event loop hoặc vòng lặp chính đứng quá `WATCHDOG_SECONDS` (backend) /
  `WORKER_WATCHDOG_SECONDS` (worker), mặc định 120 s, thì process ghi log `watchdog.stall` và thoát
  mã 70. Trong `backend`, `jobcrawler serve` thấy một process con thoát thì dừng các process còn
  lại và thoát; `restart: unless-stopped` dựng lại container. Postgres / Redis / gateway mất kết
  nối không làm process thoát (vòng lặp vẫn chạy, chỉ thử lại).
- `docker compose ps`: cột STATUS ghi `(healthy)` / `(unhealthy)`; `docker inspect <container>` xem
  lý do (output của lệnh healthcheck).

## Log

- `docker compose logs <service>` (log container xoay vòng 10 MB × 3); đặt `LOG_FORMAT=json` trong
  `backend/.env`, `worker/.env` để máy đọc được. Bảng `logs` trong Postgres giữ log của lượt chạy
  (trang lượt chạy).
- Production: `LOG_DB_LEVEL=WARNING` (bảng `logs` chỉ giữ cảnh báo / lỗi; log đầy đủ ở
  `docker compose logs`).
- Độ trễ phát hiện, hàng đợi, worker: trang Tổng quan / Hàng đợi / Workers của giao diện
  (`/api/crawl/status`).

## Redis

Compose một máy dùng **một** Redis (`REDIS_URL`) cho hàng đợi job, việc lấy trang của worker, khoá,
cài đặt, số đo độ trễ, chặn đoán mật khẩu, heartbeat, rate limit và cache (trang `http:*`, robots,
tìm nguồn, LLM, SERP, tổng quan). AOF mỗi giây, `noeviction`: đầy bộ nhớ thì lệnh ghi báo lỗi chứ
không âm thầm xoá việc; cache có hạn (TTL) nên tự dọn.

Có thể tách cache sang một Redis khác (`CACHE_REDIS_URL` trong `backend/.env`, nên `allkeys-lru`,
không cần lưu đĩa) để cache đầy không chèn ép hàng đợi. Trống = dùng chung `REDIS_URL`.

## Sao lưu Postgres

Không có sao lưu tự động. Sao lưu tay (`pg_dump -Fc`, chạy được trên Windows lẫn Linux):

```bash
docker compose exec postgres pg_dump -U jobcrawler -Fc -f /tmp/jobcrawler.dump jobcrawler
docker compose cp postgres:/tmp/jobcrawler.dump ./jobcrawler.dump
```

Khôi phục đè: dừng các thứ ghi vào DB (`docker compose stop backend worker`), chép file vào
container (`docker compose cp ./jobcrawler.dump postgres:/tmp/jobcrawler.dump`), chạy
`docker compose exec postgres pg_restore -U jobcrawler -d jobcrawler --clean --if-exists
/tmp/jobcrawler.dump`, rồi `docker compose up -d`. Chép bản dump ra ngoài máy; production nên dùng
Postgres quản lý (snapshot + PITR) — xem DEPLOY.md.

## Mã hoá trường nhạy cảm

Khoá API LLM / SERP và danh sách proxy chung (có mật khẩu) lưu trong DB dạng mã hoá AES-256-GCM
(`enc:v1:…`). Khoá ở `ENCRYPTION_KEYS` (`backend/.env`); trống thì backend tự sinh một khoá lưu trên
Redis — mất Redis là mất khoá, các giá trị đó phải nhập lại, nên production đặt `ENCRYPTION_KEYS` và
sao lưu nó riêng (không cùng chỗ với bản dump Postgres). Đổi khoá: thêm khoá mới vào **cuối**
(`ENCRYPTION_KEYS=cu,moi`), khởi động lại `backend` (mã hoá lại mọi giá trị bằng khoá mới), rồi
mới bỏ khoá cũ. Thiếu khoá giải mã một giá trị thì backend ghi log `settings.decrypt_failed` và coi
như chưa đặt cài đặt đó.

## Thêm doanh nghiệp, dữ liệu mẫu

Trang Doanh nghiệp: **Thêm doanh nghiệp** nhập tay từng doanh nghiệp (tên, MST, website, ghi chú —
`POST /api/companies`; MST / website sai thì báo lỗi), **Nạp danh sách** nạp cả file CSV (dòng sai
bị bỏ, báo trong kết quả). Cả hai không tạo trùng: đã có doanh nghiệp cùng MST, hoặc cùng tên mà
không mâu thuẫn MST / website, thì chỉ bổ sung trường còn trống. Thêm tay ghi nhật ký
`company.create`.

`jobcrawler serve` thấy bảng `companies` trống (lần đầu chạy) thì nạp 50 doanh nghiệp lớn của Việt
Nam (`backend/jobcrawler/data/demo_companies.csv`: tên + website, ghi chú "mẫu thử") để thử ngay;
scheduler build chúng ở các nhịp sau. Tắt: `DEMO_COMPANIES=false` (production). File này tải được ở
Nạp danh sách → "Tải CSV mẫu" (`GET /api/companies/import/sample.csv`, có BOM cho Excel): làm mẫu
định dạng, hoặc nạp lại (không tạo trùng).

## Bí mật

Bí mật nằm trong `backend/.env` / `worker/.env` (không commit). Muốn để ở file riêng (Docker
secrets, Vault…): mount mỗi giá trị thành một file `/run/secrets/<tên biến viết thường>`
(`gateway_token`, `llm_api_key`, `ui_password`…; worker: `gateway_token`, `worker_proxies`), service
tự đọc (`SECRETS_DIR`); biến môi trường / `.env` vẫn được ưu tiên nếu có.

## Bảo trì

Tự động mỗi giờ (`MAINTENANCE_CRON`): lượt chạy treo → `failed`, đối chiếu cột tóm tắt doanh
nghiệp, gộp việc trùng còn sót, xoá dữ liệu cũ (`failed_pages` 7 ngày, `source_fetches` / `logs`
30 ngày; `audit_events` giữ mãi), huấn luyện từ điển nén và nén lại dần. Sao lưu: xem
[Sao lưu Postgres](#sao-lưu-postgres).

```bash
uv run jobcrawler db maintain      # như cron
uv run jobcrawler db reclean       # áp lại bộ làm sạch nội dung lên việc đã lưu (sau khi đổi luật)
uv run jobcrawler db dedup         # điền dấu vân tay + gộp việc trùng
```

## Postgres

`/dev/shm` mặc định của container (64 MB) không đủ cho truy vấn / VACUUM song song trên bảng
lớn, nên compose đặt `shm_size: 512mb`. Schema dựng lại từ đầu ngày 19/10/2026
(`0001_initial_schema`), sau đó `0002_audit_events`; migration mới sinh bằng
`task backend:migration NAME="…"` (`uv run jobcrawler db revision -m "…"` trong `backend/`), không
sửa revision đã chạy.

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
