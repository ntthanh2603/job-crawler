# Giao diện jobcrawler (service `frontend`)

Giao diện vận hành: doanh nghiệp, việc làm, kiểm tay chất lượng, hàng đợi, worker, cài đặt. React 19
+ React Router + TanStack Query, shadcn/ui (Radix) + Tailwind 4, build bằng Vite. Là một service
riêng: image nginx phục vụ bản build, chuyển `/api/` tới API và gRPC của worker
(`/jobcrawler.crawl.v1.CrawlGateway/`, HTTP/2 không TLS cùng cổng) tới gateway của backend; không có
mã server.

## Phát triển

Cần Node.js theo `.nvmrc` (22) và go-task. Lệnh chạy trong `frontend/` (từ gốc repo:
`task frontend:<tên>`).

| Task | Việc |
| --- | --- |
| `task setup` | `npm ci` |
| `task dev` | Vite dev server http://localhost:5173 |
| `task build` | `tsc -b` + `vite build` vào `dist/` |
| `task typecheck` | `tsc -b` |
| `task test` | Vitest một lượt (thêm tham số sau `--`) |
| `task gen-api` | Sinh lại client API từ `../backend/openapi.json` |
| `task gen-api:check` | Kiểm tra client API đã khớp spec |
| `task check` | Mọi thứ CI chạy: typecheck, test, gen-api:check, build |
| `task e2e:install` | Tải Chromium cho Playwright (một lần) |
| `task e2e` | Kiểm thử E2E trên stack đang chạy (không thuộc `check`, xem dưới) |

Dev server chuyển `/api` tới `JOBCRAWLER_API` (mặc định `http://127.0.0.1:8000`, API chạy trên máy
hoặc trong compose) và đặt `Origin` bằng địa chỉ đó để qua kiểm tra chống CSRF của API. API đặt
`UI_PASSWORD` thì giao diện chuyển tới trang đăng nhập `/login` như khi mở qua nginx.

## Client API sinh từ spec

Spec OpenAPI do backend sở hữu: `backend/openapi.json` (commit cùng mã, backend ghi lại bằng
`backend/scripts/export_openapi.py` khi đổi API). orval (`orval.config.ts`) sinh từ spec đó hàm gọi
API, hook React Query và kiểu vào `src/lib/gen/queries.ts`; file này commit, không sửa tay. Gọi thật
qua `apiClient` (fetch, cùng origin) trong `src/lib/api.ts`; `src/lib/types.ts` đặt tên ngắn cho
kiểu sinh ra.

Đổi API: backend ghi lại spec → `task gen-api` → commit cả spec lẫn `src/lib/gen/`. Test
`src/lib/gen.test.ts` (chạy trong `task test` và `task gen-api:check`) sinh lại vào thư mục tạm và
báo lỗi khi `src/lib/gen/queries.ts` cũ hơn spec. Build không đọc spec, nên image chỉ cần
`frontend/`.

## Image Docker

`Dockerfile`, build context là `frontend/`:

1. `node:22-slim`: `npm ci` (cache npm qua BuildKit) rồi `npm run build`. Stage này chạy trên kiến
   trúc của máy build (kết quả chỉ là file tĩnh).
2. `nginxinc/nginx-unprivileged:1.30-alpine` (nginx stable, user 101, không root): `dist/` ở
   `/usr/share/nginx/html`, cấu hình từ `nginx/default.conf.template`. Nghe cổng **8080**, có
   `HEALTHCHECK` gọi `/healthz`.

| Biến | Mặc định | Ý nghĩa |
| --- | --- | --- |
| `API_UPSTREAM` | `http://backend:8000` | API của backend, dạng `scheme://host:port` (không có đường dẫn) |
| `REAL_IP_FROM` | rỗng | CIDR / IP của load balancer đứng trước `frontend`, cách nhau bằng dấu cách hoặc dấu phẩy (vd. `10.0.0.0/8, 192.168.1.10`); rỗng = không dùng. Xem [IP thật sau load balancer](#ip-thật-sau-load-balancer) |

Lúc khởi động, entrypoint của image nginx source `nginx/18-real-ip.envsh` (đặt `NGINX_REAL_IP` từ
`REAL_IP_FROM`), rồi chạy envsubst trên template (chỉ `API_UPSTREAM`, `NGINX_LOCAL_RESOLVERS` và
`NGINX_REAL_IP`, xem `NGINX_ENVSUBST_FILTER` trong Dockerfile) và ghi ra
`/etc/nginx/conf.d/default.conf`.

### Hành vi của nginx

- `GET /healthz` → `200 ok` (không phụ thuộc API, không ghi log truy cập).
- `/api/` → `API_UPSTREAM`, URI giữ nguyên:
  - `Host` giữ nguyên của trình duyệt (kèm cổng): API chặn request ghi có `Origin` khác `Host`.
  - `X-Forwarded-For` ghi đè bằng IP client `$remote_addr` (không nối thêm, để client không tự đặt
    IP giả né giới hạn đăng nhập sai theo IP; sau load balancer xem `REAL_IP_FROM`),
    `X-Forwarded-Proto`; `X-Request-Id` đi cả hai chiều và có trong log
    truy cập (`rid=`).
  - Lỗi của API (401 kèm `WWW-Authenticate` khi đặt `UI_PASSWORD`, 403, 422...) tới thẳng trình
    duyệt; nginx không chặn hay thay.
  - Body tối đa 50 MB (nhập CSV; lớn hơn nhận 413), chờ API trả lời tối đa 120 s.
  - `GET /api/events` (cập nhật trực tiếp, Server-Sent Events): location riêng không đệm, không
    nén, giữ kết nối tới 1 giờ không có dữ liệu (backend gửi `: ping` mỗi 15 s). Giao diện mở bằng
    `EventSource` (`src/lib/live.ts`, `src/components/live.tsx`); mất kết nối thì các trang poll.
  - Tên máy trong `API_UPSTREAM` phân giải lúc chạy (DNS của container, nhớ 10 s): API tạo lại với
    IP mới không cần khởi động lại `frontend`; API chưa sẵn sàng thì `/api/` trả 502, `/healthz` vẫn 200.
- `/assets/` (tên file có hash) → `Cache-Control: public, max-age=31536000, immutable`; thiếu file
  thì 404, không trả `index.html`.
- Còn lại → file tĩnh nếu có, không thì `index.html` (router phía trình duyệt), `Cache-Control:
  no-cache` để bản build mới có hiệu lực ngay.
- Nén gzip HTML, JS, CSS, JSON (kể cả JSON của API), SVG. Header bảo mật trên mọi phản hồi:
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`,
  `X-Frame-Options: DENY`.

Trong compose, `frontend` là cửa vào duy nhất của trình duyệt (cổng 8080 của container mở ra
`127.0.0.1` trên host); `backend` không mở cổng API ra host.

### IP thật sau load balancer

Mặc định nginx coi IP kết nối tới nó là IP client. Có load balancer / reverse proxy khác đứng trước
`frontend` thì đó là IP của proxy: log truy cập và API (bộ đếm đăng nhập sai theo IP) thấy mọi người dùng
chung một IP. Đặt `REAL_IP_FROM` bằng địa chỉ của proxy đó (CIDR hoặc IP, nhiều giá trị cách nhau bằng
dấu cách hoặc dấu phẩy):

- `nginx/18-real-ip.envsh` (chép vào `/docker-entrypoint.d/`, entrypoint của image source trước khi
  thay template) sinh một dòng `set_real_ip_from <mỗi CIDR>; real_ip_header X-Forwarded-For;
  real_ip_recursive on;` vào khối `server`. Rỗng thì dòng đó rỗng: cấu hình như khi không có biến.
- nginx đọc `X-Forwarded-For` từ phải sang trái, bỏ qua các địa chỉ thuộc `REAL_IP_FROM`, lấy địa
  chỉ đầu tiên không tin cậy làm `$remote_addr` (log và `X-Forwarded-For` gửi API, vẫn ghi đè). Giá
  trị client tự đặt nằm bên trái địa chỉ proxy ghi thêm nên không được dùng; chỉ tin đúng mạng của
  proxy, đừng tin mạng của client (vd. `0.0.0.0/0`).
- Proxy đứng trước phải *nối thêm* IP client vào `X-Forwarded-For` (nginx:
  `$proxy_add_x_forwarded_for`; phần lớn load balancer làm sẵn).
- Giá trị có ký tự ngoài IP / CIDR (`0-9 a-f : . /`) thì container dừng ngay lúc khởi động, log ghi
  rõ giá trị sai; CIDR viết sai thì `nginx` báo lỗi và cũng không khởi động.

## Đăng nhập và đăng xuất

Đăng nhập: API trả 401 kèm `WWW-Authenticate: Bearer` (đặt `UI_PASSWORD`) thì client API
(`lib/api.ts`) chuyển tới `/login?next=<trang đang mở>`; trang đăng nhập gọi `POST /api/auth/login`,
máy chủ đặt cookie httpOnly (giao diện không giữ token). `GET /api/me` cho biết `user`, `auth`
(`jwt` | `proxy` khi SSO qua reverse proxy | `none`) và `logout_url`. Góc dưới thanh bên hiện tên
người dùng (bấm vào: menu tài khoản); dưới đó là nút **Đăng xuất** to — `jwt`: `POST /api/auth/logout`
rồi về trang đăng nhập; SSO có `logout_url`: thẻ `<a>` thường, rời ứng dụng để proxy xoá phiên.

Cài đặt là popup mở trên trang đang xem (`?settings=<nhóm>`, `components/settings-dialog.tsx`), mở
từ menu tài khoản (bấm avatar ở cuối thanh bên → **Cài đặt**); đường dẫn cũ `/settings` mở popup trên
Tổng quan.

## Kiểm thử E2E (Playwright)

Smoke test chỉ đọc (`e2e/`, cấu hình `playwright.config.ts`, Chromium) chạy trên một stack đang
chạy; không tự khởi động stack nên không nằm trong `task check`.

```sh
npm run e2e:install   # task e2e:install — tải Chromium một lần (Linux thiếu thư viện: -- --with-deps)
npm run e2e           # task e2e — thêm tham số sau --, vd. -- --headed, -- -g "Cài đặt"
```

| Biến | Mặc định | Ý nghĩa |
| --- | --- | --- |
| `E2E_BASE_URL` | `http://127.0.0.1:8000` | Địa chỉ giao diện (compose: `frontend`; Vite dev server: `http://localhost:5173`) |
| `E2E_USER`, `E2E_PASSWORD` | rỗng | Tài khoản đăng nhập (UI_USERNAME / UI_PASSWORD của backend) khi stack bật đăng nhập |
| `CI` | rỗng | Có giá trị: cấm `test.only`, thử lại một lần, thêm báo cáo HTML `playwright-report/` |

Mỗi test tự fail khi có lỗi console hay lỗi JS chưa bắt (kể cả request API hỏng). Danh sách
(`e2e/smoke.spec.ts`):

- Khung ứng dụng tải được: thanh bên đủ mục, chọn giao diện sáng / tối, người dùng theo
  `GET /api/me` (có **Đăng xuất** trỏ tới `logout_url` khi API trả, không có khi không trả), trang
  Tổng quan.
- Tổng quan, Doanh nghiệp, Việc làm, Hàng đợi, Workers, Cài đặt, Nhật ký thao tác: mở từ thanh bên
  (điều hướng phía trình duyệt, mục đang chọn được đánh dấu, tiêu đề tab theo trang) và mở thẳng bằng
  đường dẫn; mỗi trang có tiêu đề, tải xong dữ liệu, không có khối lỗi.
- Cài đặt: mở nhóm Proxy, tìm cài đặt, tìm không thấy rồi "Xem tất cả" (chỉ lọc, không lưu).
- Doanh nghiệp: hộp thoại "Thêm doanh nghiệp" có đủ ô (mở rồi đóng, không gửi); "Nạp danh sách" có
  liên kết "Tải CSV mẫu", file tải về là CSV 50 doanh nghiệp.
- Đường dẫn không tồn tại (link cũ): trang "Không tìm thấy trang", quay về Tổng quan.
- Điện thoại 390px: Tổng quan không cuộn ngang; menu mở được và điều hướng.

Test thất bại để lại trace và ảnh chụp trong `test-results/` (`npx playwright show-trace <file>`).
