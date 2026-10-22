# Vận hành

Việc cần biết để hệ thống tự chạy ổn định. Giao diện web chỉ hiện trạng thái và cho sửa cài đặt;
lưu ý vận hành nằm ở đây. Kiến trúc: [ARCHITECTURE.md](ARCHITECTURE.md). Nhiều máy và quy mô:
[DEPLOY.md](DEPLOY.md).

## 1. Chạy

```bash
task setup   # dependency + backend/.env, worker/.env từ .env.example của từng service
task up      # 5 container: postgres, redis, backend, frontend, worker (giao diện 127.0.0.1:8000)
task down
```

- Compose dev dùng cổng, tài khoản Postgres và một worker cố định trong `docker-compose.yaml`.
  `backend/.env`: kết nối DB / Redis, gateway, đăng nhập, khoá mã hoá, log. `worker/.env`: địa chỉ
  gateway, khoá tham gia, proxy riêng của máy.
- Mọi cài đặt khác sửa ở trang **Cài đặt** (lưu trong DB, thắng biến môi trường, mọi process nạp
  lại ≤ 5 s). Mục có ↻ chỉ đọc lúc khởi động: lưu xong `jobcrawler serve` tự khởi động lại các process
  con trong vài giây (job đang chạy được xếp lại). Giá trị còn đặt bằng biến môi trường thì nút **Lưu
  vào cơ sở dữ liệu** chép vào DB, sau đó xoá khỏi `.env` được.
- Đổi mã nguồn: `task up` (build lại image; migration chạy lúc khởi động).
- Postgres (`127.0.0.1:15432`) và Redis (`127.0.0.1:16379`) chỉ mở ở máy host. Trên Windows dùng
  `127.0.0.1`, không dùng `localhost` (thử IPv6 trước, mỗi kết nối chậm ~2 s).
- Lệnh `jobcrawler …` chạy trong `backend/`: `task backend:cli -- <lệnh>`.

## 2. Lịch thu thập

| Khi nào                               | Việc                                                                                                        |
| ------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| Doanh nghiệp mới (thêm tay / nạp CSV) | build ngay, bất kể giờ                                                                                      |
| Đêm `BUILD_WINDOW` 00:00–06:00        | build mỗi doanh nghiệp một lần: đọc đủ site, dựng lại bộ khung, đóng việc đã gỡ                             |
| Ngày `POLL_WINDOW` 06:00–24:00        | poll mỗi 150 s ± 10 s theo bộ khung, chỉ mở trang tin mới                                                   |
| Poll lỗi (bộ khung hỏng)              | build lại ngay; vẫn hỏng thì thử lại sau 2 giờ (`REBUILD_ON_ERROR` tắt: chờ build đêm, nguồn khác vẫn poll) |
| Trang tuyển dụng có tin mới           | poll ngay doanh nghiệp có profile đó (đang có lượt poll thì lượt đó đọc tin)                                |

Khung giờ theo `CRAWL_TIMEZONE` (Asia/Ho_Chi_Minh). Đổi khung / chu kỳ có hiệu lực trong vài giây;
đổi múi giờ (↻) thì `jobcrawler serve` khởi động lại các process con, vì lịch cron của runner tính
theo múi giờ đọc lúc khởi động. Biểu đồ "việc mới theo ngày" ở Tổng quan cũng chia ngày theo múi
giờ này.

**Bộ khung** (trang Cài đặt → Lịch thu thập): mỗi doanh nghiệp tối đa `PLAN_MAX_PAGES` (10) trang
mỗi lượt poll, mỗi nguồn ít nhất `PLAN_MIN_PAGES` (3) khi site có đủ. Tab "Bộ khung & lịch" của doanh
nghiệp vẽ đường một lượt poll của từng nguồn (đọc danh sách → so mã tin → mở chi tiết tin mới → kết
quả lượt gần nhất) và ghi "Đọc trọn N trang/lượt" hoặc "Đọc 10/14 trang/lượt" (site dài hơn bộ khung:
poll chỉ thấy tin mới ở các trang đầu, không đóng việc; build đêm đọc đủ và đóng). Nút "Bản đồ bộ
khung" mở toàn bộ một bộ khung: cây trang → API, các mã tin kèm tin đã lưu, link bỏ qua, danh mục
mã → tên, JSON gốc.

**Khi nào poll đóng việc**: chỉ khi nguồn được đọc trọn (mọi trang, không lỗi, không bị cắt) và tin
vắng mặt không phải tin "gián tiếp". Mất ≥ 30% tin trong một lượt thì không đóng mà đếm như lỗi; lặp
3 lượt thì build lại.

## 3. Độ trễ phát hiện tin mới (P95 ≤ 3 phút)

Trang **Tổng quan** → "Độ trễ phát hiện tin mới":

- **Cửa sổ phát hiện** P95 phải ≤ 180 s. Bình thường ≈ 0,95 × 150 + 10–20 ≈ 155–165 s.
- **Độ tươi**: tuổi lượt poll đủ gần nhất của từng doanh nghiệp; "quá hạn" > 0 là có doanh nghiệp
  chưa được poll kịp.
- Chỉ tính trong khung poll. Tin đăng 00:00–06:00 được thấy ở lượt build đêm của doanh nghiệp hoặc
  lượt poll đầu lúc 06:00. Muốn ≤ 3 phút cả đêm: đặt `POLL_WINDOW` 00:00–24:00 và thêm năng lực cho
  cả poll lẫn build ban đêm (DEPLOY.md).

| Dấu hiệu                                                     | Nguyên nhân thường gặp                                    | Làm gì                                                       |
| ------------------------------------------------------------ | --------------------------------------------------------- | ------------------------------------------------------------ |
| Hàng đợi poll có việc **chờ**                                | thiếu slot runner poll                                    | thêm worker; chạm "Job poll song song tối đa" thì tăng nó    |
| Trang Workers: slot HTTP đang dùng sát tổng                  | thiếu worker                                              | thêm worker                                                  |
| P95 thời gian một lượt poll tăng, request lỗi / quá giờ tăng | proxy chậm / bị chặn                                      | đổi proxy, thêm proxy                                        |
| P95 > 180 s mà hàng đợi rỗng                                 | lượt poll dài (rate limit 1 req/s / domain × nhiều trang) | giảm `PLAN_MAX_PAGES` hoặc `POLL_INTERVAL_SECONDS` (vd. 120) |

Đo đầu-cuối trên site giả (không phụ thuộc site thật):

```bash
cd backend
uv run python scripts/loadtest/sla_site.py --port 8765 --every 75 --count 16 --out sla.jsonl
# thêm doanh nghiệp website http://host.docker.internal:8765/, chờ build xong
uv run python scripts/loadtest/sla_report.py sla.jsonl
```

## 4. Proxy

Trang Cài đặt → **Proxy**.

- **Chế độ "Worker tự xoay"** (mặc định): mỗi worker tải danh sách proxy chung về (qua gateway, khi
  đăng ký) và tự xoay: mỗi domain giữ một proxy 10 phút rồi sang proxy kế tiếp, mỗi worker bắt đầu ở
  một vị trí khác nhau; proxy bị chặn (403 / 407 / 429) nghỉ với domain đó 2 phút; 5 lỗi kết nối liên
  tiếp → loại 10 phút. Máy worker có proxy riêng (`WORKER_PROXIES` / `WORKER_PROXIES_FILE` trong
  `worker/.env`) dùng proxy của nó. Danh sách trống và không có proxy riêng → đi thẳng.
- **"Backend chọn"**: backend chọn proxy cho từng request theo điểm, giữ theo domain 30 phút, trạng
  thái trên Redis; dùng khi muốn một nơi quyết định duy nhất.
- **Bảng proxy**: thêm / sửa / xoá (HTTP, HTTPS, SOCKS; tài khoản, mật khẩu), "Dán nhiều dòng"
  (`http://user:pass@host:port`, `host:port`, `host:port:user:pass`). Mật khẩu không bao giờ trả lại
  giao diện, lưu mã hoá. Lưu xong worker nhận danh sách mới trong khoảng 15 s.
- **Kiểm tra**: worker gọi trang kiểm tra qua từng proxy (20 proxy mỗi lần), báo độ trễ, IP ra, quốc
  gia; proxy sống được đưa lại vào vòng xoay, proxy lỗi bị loại.
- Danh sách chung có mật khẩu được gửi xuống mọi worker: gateway mở ra ngoài thì luôn bật TLS.
- Trang tuyển dụng chặn IP thẳng (TopCV: Cloudflare, CareerLink: CAPTCHA): cho các domain này đi pool
  worker có proxy dân cư (`FETCH_ROUTES={"topcv.vn": "residential"}` + `WORKER_POOL=residential`).

## 5. Worker

- Trang **Workers**: worker đang sống, pool, slot HTTP / render đang dùng, proxy, việc xong / lỗi, CPU /
  RAM; "Dừng nhận việc" để worker làm nốt rồi thoát (dùng khi thay proxy / nâng cấp).
- **Thêm máy worker**: trang Workers → "Thêm worker" tạo khoá tham gia **dùng một lần** (15 phút;
  đóng hộp thoại là huỷ) và lệnh `docker pull` + `docker run` để chạy trên máy worker. Địa chỉ
  (worker nối gRPC qua cổng giao diện), image: Cài đặt → Worker lấy trang (`GATEWAY_PUBLIC_URL`,
  trống = địa chỉ đang mở giao diện; `GATEWAY_WORKER_IMAGE`, mặc định tag `main` vì GHCR chưa có
  `latest`). Địa chỉ `localhost` được đổi thành `host.docker.internal`, chỉ dùng được trên chính máy
  backend; máy khác cần `WEB_BIND=0.0.0.0` hoặc load balancer. CA tự ký: thêm
  `GATEWAY_CA_CERT` vào lệnh. Xoá container rồi tạo lại cần khoá mới. Worker trong compose dùng
  `GATEWAY_TOKEN` (cùng giá trị ở `backend/.env` và `worker/.env`). Thu hồi một worker: xoá
  `<prefix>:gw:wtoken:*` của nó trên Redis.
- Log worker `ALREADY_EXISTS` ("worker_id đang được worker khác dùng"): id đó đang sống dưới token
  khác. Worker vừa mất file token (container tạo lại với `WORKER_ID` cố định) tự vào được khi bản cũ
  hết hạn báo sống (`GATEWAY_WORKER_TTL_SECONDS`, 30 s); gặp mãi là hai worker đặt trùng
  `WORKER_ID` — đổi một bên.
- Máy worker cần đồng bộ giờ (NTP): worker so hạn của việc với đồng hồ máy nó, máy nhanh giờ vài
  phút bỏ việc oan (`expired`), chậm giờ thì tải cả việc backend đã thôi chờ.
- Không có worker nào → mọi request lỗi `no_worker`; worker quay lại thì tự chạy tiếp.
- Slot: cấu hình ở trang Cài đặt → "Worker lấy trang" là **mức trần**; worker tự tính theo máy khi
  `WORKER_HTTP_SLOTS` / `WORKER_RENDER_SLOTS` trống. Chromium lỗi → worker tạm tắt render rồi thử lại
  (30 s → 10 phút).

## 6. Đăng nhập và bí mật

- Đặt `UI_PASSWORD` (+ `UI_USERNAME`, `JWT_SECRET`) trong `backend/.env` → trang đăng nhập, phiên JWT
  12 giờ (`AUTH_TOKEN_HOURS`). Đổi mật khẩu / `JWT_SECRET` → mọi phiên cũ hết dùng. Script:
  `POST /api/auth/login` lấy `access_token`, gửi `Authorization: Bearer`. Để trống `UI_PASSWORD` chỉ
  khi cổng chỉ mở ở 127.0.0.1.
- `frontend` đứng sau load balancer khác: đặt `REAL_IP_FROM` (`.env` gốc) = CIDR của nó, không thì mọi
  người dùng chung bộ đếm chặn đoán mật khẩu.
- SSO (nhiều người vận hành): proxy SSO trước `frontend`, đặt `AUTH_PROXY_USER_HEADER` (và
  `AUTH_LOGOUT_URL`).
- **Mã hoá**: khoá API LLM / SERP và danh sách proxy chung lưu mã hoá AES-256-GCM bằng
  `ENCRYPTION_KEYS`. Trống thì backend tự sinh khoá trên Redis — mất Redis là mất khoá, nên production
  phải đặt và sao lưu riêng. Đổi khoá: thêm khoá mới vào **cuối** (`ENCRYPTION_KEYS=cu,moi`), khởi động
  lại `backend` (mã hoá lại), rồi mới bỏ khoá cũ.
- Bí mật dạng file: mount `/run/secrets/<tên biến viết thường>` (`gateway_token`, `ui_password`…), service
  tự đọc (`SECRETS_DIR`); biến môi trường vẫn được ưu tiên.

## 7. Doanh nghiệp và dữ liệu mẫu

- Trang **Doanh nghiệp**: "Thêm doanh nghiệp" (nhập tay; MST / website sai thì báo lỗi) hoặc "Nạp danh
  sách" (CSV cột `name`, `tax_code`, `website`, `note`; dòng sai bị bỏ). Không tạo trùng: cùng MST,
  hoặc cùng tên không mâu thuẫn MST / website → chỉ bổ sung trường trống. Nạp mỗi lô 500 dòng một
  transaction; lô đụng deadlock với lượt KYC đang chạy được chạy lại (tối đa 3 lần, log
  `import.deadlock_retry`).
- DB trống lúc khởi động thì tự nạp 50 doanh nghiệp lớn (`DEMO_COMPANIES=true`, tắt ở production).
  File đó tải được ở "Nạp danh sách" → "Tải CSV mẫu".

## 8. Sức khoẻ, log, bảo trì

- Healthcheck: `backend` gộp `/healthz`, `jobcrawler gateway-health`, `jobcrawler health scheduler
runner` (heartbeat Redis mỗi 5 s); `worker`: `jobworker health` (mất gateway quá 5 phút mới hỏng).
  Docker chỉ đánh dấu unhealthy; process treo thì watchdog cho nó thoát (mã 70) và
  `restart: unless-stopped` dựng lại.
- Log: `docker compose logs <service>` (xoay 10 MB × 3); `LOG_FORMAT=json` cho máy đọc. Bảng `logs` giữ
  log của lượt chạy; production đặt `LOG_DB_LEVEL=WARNING`. Mỗi request API có `X-Request-Id`, lỗi 500
  trả `request_id` để tra log.
- **Nhật ký thao tác**: ai lưu cài đặt / bấm build / thêm doanh nghiệp / xuất dữ liệu / đăng nhập
  (kể cả đăng nhập sai)…, lúc nào, từ IP và trình duyệt nào, giá trị trước → sau (bí mật bị che).
  Chỉ ghi thêm, bảo trì không dọn. Dòng ghi trước 21/10/2026 (revision 0004) không có IP / trình
  duyệt.
- **Log về chia việc** (cảnh báo bất thường, không cần làm gì nếu chỉ thỉnh thoảng):
  `queue.job_revoked` (runner tự huỷ lượt đã bị runner khác thu hồi — runner đó từng đứng / mất
  Redis), `lock.lost` + `LeaseLost` (lượt mất khoá doanh nghiệp / bảo trì giữa chừng;
  `board.index_lock_lost`: lượt index trang tuyển dụng mất khoá, bị huỷ), `FencedOut`
  (lượt cũ bị chặn ghi DB vì lượt mới đã giữ khoá), `company.build_skipped` (build gặp lượt khác
  đang chạy), `gateway.worker_lost` với `reason=orphan` (worker không nhận được việc đã giao) và
  `gateway.revoked`. Lặp lại thường xuyên → xem runner / worker đó có bị đứng (CPU, swap, GC) hay mạng
  tới Redis / gateway chập chờn.
- **Bảo trì tự động** mỗi giờ: lượt chạy treo → `failed`, gộp việc trùng còn sót, xoá dữ liệu cũ
  (`failed_pages` 7 ngày, `source_fetches` / `logs` 30 ngày), nén lại dần. Tay:
  `jobcrawler db maintain`, `db reclean` (sau khi đổi luật làm sạch), `db reclassify` (sau khi đổi
  luật ngành nghề / học vấn; `--dry-run` chỉ đếm), `db dedup`, `index-boards --rematch` (sau khi đổi
  luật so tên profile).
- **Ngành nghề, học vấn, số lượng tuyển** (revision 0005): việc lưu trước đó để trống tới lượt chạy
  sau của doanh nghiệp; muốn có ngay thì chạy một lần `jobcrawler db reclassify` (đọc nội dung thô
  của tin đã lưu, không tải lại trang). Học vấn / số lượng tuyển / danh mục nghề TopCV đọc từ HTML
  chỉ có ở tin được tải lại sau khi nâng cấp (tin mới, tin đổi, tin quá hạn làm mới); trước đó ngành
  lấy từ `industry` của JSON-LD đã lưu.

## 9. Redis và Postgres

- Một Redis (`REDIS_URL`) cho hàng đợi, việc lấy trang, khoá, cài đặt, số đo, rate limit; AOF mỗi
  giây, `noeviction` (đầy thì báo lỗi, không âm thầm xoá việc). Cache (trang, tìm nguồn, LLM, SERP,
  tổng quan) tách sang Redis khác được: `CACHE_REDIS_URL` (`allkeys-lru`, không cần lưu đĩa).
- Postgres: compose đặt `shm_size: 512mb`; migration mới bằng `task backend:migration NAME="…"`, không
  sửa revision đã chạy. Sao lưu tay:

  ```bash
  docker compose exec postgres pg_dump -U jobcrawler -Fc -f /tmp/jobcrawler.dump jobcrawler
  docker compose cp postgres:/tmp/jobcrawler.dump ./jobcrawler.dump
  ```

  Khôi phục: `docker compose stop backend worker`, chép file vào container, `pg_restore --clean
--if-exists -U jobcrawler -d jobcrawler /tmp/jobcrawler.dump`, `docker compose up -d`. Production dùng
  Postgres quản lý (snapshot + PITR).
- Compose chạy Postgres 18 (`pgvector/pgvector:pg18`, volume `pgdata` mount ở `/var/lib/postgresql`).
  Volume tạo từ bản 16 không dùng tiếp được (khác major, khác chỗ đặt dữ liệu): sao lưu như trên bằng
  container cũ, `docker compose down`, `docker volume rm jobcrawler_pgdata`, `docker compose up -d`
  (migration chạy khi `backend` khởi động; script `deploy/postgres/init` tạo lại `jobcrawler_test`), rồi
  khôi phục bằng `pg_restore`.

## 10. Sự cố hay gặp

- **Docker Desktop tắt** (máy ngủ / cập nhật): mở lại, container tự lên; worker tự nối lại gateway.
- **LLM lỗi** (503…): mỗi lời gọi tốn ~4 s rồi bỏ; KYC ra `ambiguous`, tin cần LLM bị hoãn (lượt sau
  chấm lại). Kiểm tra: `jobcrawler llm check`. Trang **Tổng quan** → tab **LLM**: tỉ lệ thành công,
  P50 / P95 thời gian, lỗi theo nguyên nhân, số lần 429 và từng bước (prompt). Nhãn "Tạm dừng" là cả
  cụm đang chờ sau 429 vượt request/phút. Số lời gọi đồng thời của cả cụm có hàng chờ chung (Cài
  đặt → LLM & tìm kiếm → "Request LLM song song cả cụm", tự hạ khi endpoint báo quá giới hạn — log
  `semaphore.shrink` kèm số chỗ còn lại); lỗi "chờ lượt LLM chung quá … s" nhiều → endpoint không kịp
  với số build đang chạy: giảm "Job build song song" hoặc tăng năng lực LLM. "Chờ lượt quá lâu" /
  nhiều 429 → giảm `LLM_REQUESTS_PER_MINUTE` hoặc "Request LLM song song cả cụm" cho khớp giới hạn
  của endpoint; "Quá giờ" nhiều → model chậm, tăng `LLM_TIMEOUT_SECONDS` hoặc giảm tải; "JSON sai
  schema" tăng sau khi đổi `LLM_MODEL` → model mới không theo được structured output.
- **Redis không lên sau khi tắt đột ngột** (log "Bad file format reading the append only file"):

  ```bash
  docker compose stop redis
  docker compose run --rm --no-deps -T --entrypoint sh redis -c \
    'b=/data/aof-backup-$(date +%Y%m%d-%H%M%S); mkdir -p $b && cp -a /data/appendonlydir/. $b/ &&
     cd /data/appendonlydir && echo y | redis-check-aof --fix appendonly.aof.manifest'
  docker compose up -d
  ```

- **Test tích hợp chập chờn trên Windows** (`WinError 64`): chạy lại file đó. Các bộ test tích hợp dùng
  chung DB `jobcrawler_test`: không chạy song song.
