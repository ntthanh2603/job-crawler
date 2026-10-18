# Vận hành

Ghi chú để hệ thống tự chạy ổn định: dịch vụ nào chạy gì, giới hạn đang đặt, cách chịu dữ liệu
lớn, và những việc cần làm khi đổi cấu hình. Giao diện web chỉ hiện **trạng thái**; mọi lưu ý vận
hành nằm ở đây.

## Dịch vụ (docker compose)

| Dịch vụ | Việc | Số bản |
|---|---|---|
| `postgres` | dữ liệu (`shm_size: 512mb` — xem [Postgres](#postgres)) | 1 |
| `redis` | hàng đợi SAQ, cache tổng quan, khoá phân tán | 1 |
| `migrate` | `jobcrawler db upgrade` mỗi lần `up` (không có việc thì thoát) | — |
| `worker` | `process_company`, cron bảo trì (mỗi giờ), cron số liệu tổng quan (5 phút) | `WORKER_REPLICAS` |
| `indexer` | index tăng dần các trang tuyển dụng (cron mỗi 5 giờ + nút "Index ngay") | `INDEXER_REPLICAS` |
| `api` | API + giao diện React, `127.0.0.1:${API_PORT:-8000}` | 1 |

Đổi mã nguồn: `docker compose build && docker compose up -d` (migration chạy tự động).

## Index trang tuyển dụng — đang chạy KHÔNG proxy

Indexer đang bật (`INDEXER_REPLICAS=1` trong `.env`). Chưa có `config/proxies.txt` nên mọi request
đi thẳng từ một IP, vì vậy `.env` đặt giới hạn thận trọng:

```env
BOARD_CONCURRENCY=2          # mặc định 4 — số request song song tới MỘT trang
BOARD_MAX_JOBS_PER_RUN=1000  # mặc định 20.000 — số trang chi tiết tải mỗi lượt
```

Mỗi lượt vẫn tôn trọng `robots.txt` (kể cả `Crawl-delay`), lùi lại khi gặp 429 và dừng khi bị
chặn (CAPTCHA / 403 liên tiếp). Với giới hạn này mỗi trang chỉ được index
dần (1.000 tin mới / đổi mỗi 5 giờ), nên các trang lớn (TopCV ~45 nghìn tin) cần nhiều lượt mới đủ.

**Khi có proxy:** tạo `config/proxies.txt` theo mẫu `config/proxies.example.txt` (`PROXY_ENABLED=true`
đã bật sẵn), xoá hai dòng `BOARD_*` trên (về mặc định), rồi `docker compose up -d`. Theo dõi tỉ lệ "Bị chặn" ở biểu đồ
"Request 24 giờ qua" trên trang Tổng quan.

**Tắt hẳn:** `INDEXER_REPLICAS=0` rồi `docker compose up -d`.

## Thu thập đủ việc của một doanh nghiệp

Một lượt `process_company` có `COMPANY_DEADLINE_SECONDS` (165 giây), nên doanh nghiệp lớn không
xong trong một lượt. Hệ thống đi tiếp qua nhiều lượt thay vì bỏ dở:

- **Lượt bị cắt là `partial`, không đóng việc cũ.** Nguồn chạm giới hạn (trang chi tiết, trang
  danh sách, trang API, trình duyệt, `SOURCE_MAX_JOBS`), trang lỗi tạm (timeout, 5xx), tìm nguồn
  lỗi, hay còn tin chờ LLM chấm (`verify`) đều ghi vào `incomplete_sources`; chỉ lượt đủ nguồn mới
  đánh `removed` các việc không còn thấy.
- **Tự chạy tiếp.** Lượt `partial` tự xếp lượt sau (`COMPANY_MAX_CONTINUATIONS`, mặc định 2, cách
  `COMPANY_CONTINUATION_DELAY_SECONDS` = 60 giây). Lượt sau dùng lại chi tiết tin website/ATS đã
  tải trong `JOB_REUSE_DAYS` (3 ngày) và cache HTTP (1 giờ) nên đi tiếp tới tin chưa tải; giới
  hạn `SOURCE_MAX_DETAIL_PAGES` chỉ tính trang tải thật. Dữ liệu trước khi có cơ chế này chưa có
  `fetched_at` nên được tải lại một lần.
- **Tin LLM chưa kịp chấm** được hoãn (`DEFERRED`), không bị loại: việc cũ giữ nguyên, lượt sau
  chấm tiếp (câu trả lời có cache). Không cấu hình LLM thì tin nghi ngờ vẫn bị loại như trước.
- **Trang tuyển dụng song song với website.** Profile trên trang tuyển dụng được thu thập ngay
  khi lượt chạy bắt đầu, không chờ bước tìm nguồn trên website.
- **Trang careers dựng bằng JavaScript (SPA).** Khi mở bằng trình duyệt, hệ thống giữ các phản
  hồi JSON trang gọi (tối đa 30 phản hồi, 2 MB mỗi cái) và đọc tin từ đó cùng JSON nhúng
  (`__NEXT_DATA__`), rồi đọc tiếp các trang sau của API (`SOURCE_MAX_API_PAGES`, mặc định 20) chỉ
  với header `Accept`, không cookie. Trang danh mục / theo tỉnh dẫn tới tin (vd. Thế Giới Di Động)
  được đi tiếp, không tính vào giới hạn trang chi tiết (tối đa `SOURCE_MAX_SUB_LISTINGS` = 60).

### Trang tuyển dụng

- **Index chưa đủ thì đọc trực tiếp trang profile.** Profile chỉ được coi là "index còn mới" khi
  lượt index đọc đủ danh sách và không còn tin chưa tải của profile đó. Với giới hạn
  `BOARD_MAX_JOBS_PER_RUN=1000` hiện tại (chưa có proxy) phần lớn profile của TopCV, JobsGO,
  CareerViet, VietnamWorks chưa đủ, nên `process_company` đọc danh sách tin trên trang profile
  (đủ các trang), dùng lại tin đã index và chỉ tải tin còn thiếu.
- **Sau mỗi lượt index**, doanh nghiệp có profile vừa có tin mới (hoặc vừa map) được xếp chạy lại
  nếu lượt gần nhất cũ hơn `BOARD_RERUN_MIN_HOURS` (6 giờ). Tắt: `BOARD_RERUN_COMPANIES=false`.
- **Khớp tên profile** dùng khoá riêng (`company_match_key`: mở rộng "TMCP", bỏ phần trong ngoặc,
  phần chi nhánh, đuôi "Việt Nam"/"Group"). Sau khi cập nhật mã nguồn, chạy một lần để tính lại
  khoá cho dữ liệu cũ và map lại profile chưa map (có thể gọi LLM nhiều ở lần đầu):

  ```bash
  uv run jobcrawler index-boards --rematch
  ```
- **Doanh nghiệp KYC không tìm thấy website** vẫn dùng profile có tên trùng hẳn tên doanh nghiệp
  (đủ riêng, khớp bằng rule); doanh nghiệp KYC mơ hồ chỉ dùng profile khớp MST/website. Tắt:
  `BOARD_NAME_PROFILES_WITHOUT_KYC=false`.

### Giới hạn còn lại

- **TopCV** chặn bằng Cloudflare khi đi thẳng (mỗi lượt index chỉ tải được ~20 tin), **CareerLink**
  hiện CAPTCHA sau vài chục request: cần proxy (mục trên) mới index đủ. Đọc trực tiếp trang
  profile vẫn chạy được ở mức vài request mỗi doanh nghiệp.
- **API careers toàn cầu** (vd. Shopee trả tin mọi quốc gia, xếp Việt Nam trước): mỗi nguồn dừng ở
  `SOURCE_MAX_JOBS` (500), nơi làm đọc từ danh mục mã của trang nếu có.

## Chịu dữ liệu lớn

Mục tiêu: mọi thao tác trên giao diện dưới ~300 ms khi có hàng triệu việc làm.

- **ID dạng UUIDv7** (migration 0007): khoá chính mọi bảng là UUID gồm 48 bit thời gian + phần
  ngẫu nhiên, sinh ở ứng dụng (`db/ids.py`, tăng chặt trong một process) hoặc bằng `uuid_v7()` của
  DB khi insert SQL thô. Sinh được ở mọi worker không cần hỏi DB, không lộ số lượng dòng, vẫn tăng
  theo thời gian nên index ghi nối đuôi như bigint và `ORDER BY id` theo thứ tự tạo. Dữ liệu trước
  0007 được đổi tất định (giữ thứ tự và quan hệ); `jobcrawler db downgrade 0006` đổi ngược về đúng
  id số cũ. Link cũ dạng `/companies/123` báo "không tìm thấy". Riêng `compression_dicts.id` vẫn
  là số: đó là mã từ điển ghi trong từng giá trị nén.
- **Không trang nào tải cả bảng.** Mọi danh sách phân trang ở server (50 dòng/trang): việc làm,
  doanh nghiệp, việc làm / lượt chạy / tin bị loại của một doanh nghiệp, request / log của một lượt
  chạy, lịch sử index, hàng đợi (25 việc/trang mỗi hàng đợi).
- **Đếm có giới hạn.** `COUNT(*)` trên bảng lớn có lọc rất tốn: server đếm tối đa 10.000 dòng
  (`queries.COUNT_CAP`), giao diện hiện "10.000+" và chỉ cho đi tới trang 200 — muốn xem tiếp thì
  lọc hẹp lại.
- **Index theo đúng truy vấn** (migration 0006): mỗi kiểu sắp xếp việc làm có index
  `(status, khoá sắp xếp, id)`; lọc nguồn theo biểu thức `split_part(canonical_source, ':', 1)`;
  tỉnh/thành bằng GIN `jsonb_path_ops`; tìm tiêu đề / tên doanh nghiệp bằng GIN trigram; BRIN theo
  thời gian cho biểu đồ.
- **Không truy vấn con theo từng dòng.** Lượt chạy mới nhất và số việc đang mở ghi sẵn trên
  `companies` (`last_run_*`, `active_jobs`) — cập nhật khi lượt chạy bắt đầu / kết thúc và sau mỗi
  lần lưu việc làm; bảo trì mỗi giờ đối chiếu lại (`refresh_company_summaries`). Tìm doanh nghiệp
  qua cột sinh `search_text` + bảng tên gọi khác, hai nhánh index hợp bằng `UNION`.
- **Tổng quan không quét lại mỗi lần mở.** Phân bố trên toàn bộ việc làm (nguồn, tỉnh, cấp bậc,
  lương, KYC…) do cron `overview_cron` tính mỗi 5 phút, lưu Redis; phần theo cửa sổ thời gian chỉ
  đọc dòng trong cửa sổ và cache 60 giây. Khoá Redis có tiền tố tên queue để môi trường / test khác
  không đọc nhầm của nhau.

### Số đo (dữ liệu giả lập)

`scripts/loadtest/seed.py` tạo DB `jobcrawler_load` với 100 nghìn doanh nghiệp, 1 triệu việc làm,
1 triệu lượt chạy, 2 triệu request, 1 triệu tin nguồn (id UUIDv7); `scripts/loadtest/bench.py
--prepare` đo (máy dev, Postgres trong Docker Desktop, 18/10/2026 — sau khi đổi id sang UUID; số
đo với id bigint hôm trước tương đương):

| Truy vấn | Thời gian |
|---|---|
| Việc làm: mặc định / trang 150 / sắp theo lương / sắp hết hạn | 10 / 19 / 23 / 9 ms |
| Việc làm: tìm "kế toán" (khớp 10% bảng) / tìm tên doanh nghiệp | 188 / 24 ms |
| Việc làm: lọc tỉnh / nguồn + cấp bậc | 199 / 247 ms |
| Doanh nghiệp: mặc định / tìm tên / tìm MST / lọc lượt cuối | 4 / 23 / 9 / 6 ms |
| Trang doanh nghiệp + các tab, trang lượt chạy | 2–8 ms |
| Tổng quan: lượt chạy / request theo giờ, thống kê 24 giờ | 15–107 ms |
| Tổng quan: phân bố (chạy nền bằng cron, người xem không chờ) | 2,5 s |

Chạy lại sau khi đổi truy vấn: `uv run python scripts/loadtest/seed.py` (≈ 8 phút) rồi
`uv run python scripts/loadtest/bench.py --prepare`. Truy vấn nào > 300 ms được liệt kê cuối.

## Không có việc làm trùng

- **Trong một lượt chạy**, các tin cùng việc được gộp thành một (`pipeline/dedup.py`): cùng tiêu
  đề, cùng nơi làm, JD giống nhau; tin cùng nguồn không ghi nơi làm mà tiêu đề + JD gần như y hệt
  là tin đăng lại với mã mới (MISA `/tuyen-dung/5883` và `/5920`) → một việc.
- **Với việc đã lưu**, tin mới được gắn vào việc đang mở cùng dấu vân tay nội dung (tiêu đề + nơi
  làm + JD), hoặc khớp mờ cùng luật trên, thay vì tạo việc mới.
- **Ở tầng DB**, index duy nhất `ux_jobs_company_fingerprint_active` chặn hai việc đang mở y hệt
  trong một doanh nghiệp (kể cả khi hai đường ghi chạy song song).
- Dữ liệu cũ: `jobcrawler db dedup` điền dấu vân tay và gộp việc trùng (việc cũ nhất giữ lại, tin
  của việc trùng chuyển sang); bảo trì mỗi giờ cũng làm việc này cho dòng chưa có dấu vân tay.

## Bảo trì

Tự động mỗi giờ (`MAINTENANCE_CRON`): lượt chạy treo → `failed`, đối chiếu cột tóm tắt doanh
nghiệp, gộp việc trùng còn sót, xoá dữ liệu cũ (`failed_pages` 7 ngày, `source_fetches` / `logs`
30 ngày), huấn luyện từ điển nén và nén lại dần.

Chạy tay khi cần:

```bash
uv run jobcrawler db maintain      # như cron
uv run jobcrawler db reclean       # áp lại bộ làm sạch nội dung lên việc đã lưu (sau khi đổi luật)
uv run jobcrawler db dedup         # điền dấu vân tay + gộp việc trùng
```

## Postgres

`/dev/shm` mặc định của container (64 MB) không đủ cho truy vấn / VACUUM song song trên bảng
lớn ("could not resize shared memory segment … No space left on device"), nên compose đặt
`shm_size: 512mb`. Thay đổi này cần tạo lại container: `docker compose up -d postgres`.

## Sự cố hay gặp

- **Docker Desktop tắt** (máy ngủ / cập nhật): cả Postgres, Redis, API và worker dừng. Mở lại
  Docker Desktop; các container có `restart: unless-stopped` tự lên.
- **Test tích hợp chập chờn trên Windows** (`WinError 64` khi nối Redis `localhost:16379`), hay
  gặp ngay sau khi Docker Desktop khởi động lại: chạy lại từng file test là qua.
