# Giao diện jobcrawler (service `web`)

Giao diện vận hành: doanh nghiệp, việc làm, kiểm tay chất lượng, hàng đợi, worker, cài đặt. React 19
+ React Router + TanStack Query, shadcn/ui (Radix) + Tailwind 4, build bằng Vite. Là một service
riêng: image nginx phục vụ bản build và chuyển `/api/` tới API của backend; không có mã server.

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

Dev server chuyển `/api` tới `JOBCRAWLER_API` (mặc định `http://127.0.0.1:8000`, API chạy trên máy
hoặc trong compose) và đặt `Origin` bằng địa chỉ đó để qua kiểm tra chống CSRF của API. API đặt
`UI_PASSWORD` thì trình duyệt hỏi mật khẩu (HTTP Basic) như khi mở qua nginx.

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
| `API_UPSTREAM` | `http://api:8000` | API của backend, dạng `scheme://host:port` (không có đường dẫn) |

Lúc khởi động, entrypoint của image nginx chạy envsubst trên template (chỉ `API_UPSTREAM` và
`NGINX_LOCAL_RESOLVERS`, xem `NGINX_ENVSUBST_FILTER` trong Dockerfile) rồi ghi ra
`/etc/nginx/conf.d/default.conf`.

### Hành vi của nginx

- `GET /healthz` → `200 ok` (không phụ thuộc API, không ghi log truy cập).
- `/api/` → `API_UPSTREAM`, URI giữ nguyên:
  - `Host` giữ nguyên của trình duyệt (kèm cổng): API chặn request ghi có `Origin` khác `Host`.
  - `X-Forwarded-For` ghi đè bằng IP client (không nối thêm, để client không tự đặt IP giả né giới
    hạn đăng nhập sai theo IP), `X-Forwarded-Proto`; `X-Request-Id` đi cả hai chiều và có trong log
    truy cập (`rid=`).
  - Lỗi của API (401 kèm `WWW-Authenticate` khi đặt `UI_PASSWORD`, 403, 422...) tới thẳng trình
    duyệt; nginx không chặn hay thay.
  - Body tối đa 50 MB (nhập CSV; lớn hơn nhận 413), chờ API trả lời tối đa 120 s.
  - Tên máy trong `API_UPSTREAM` phân giải lúc chạy (DNS của container, nhớ 10 s): API tạo lại với
    IP mới không cần khởi động lại `web`; API chưa sẵn sàng thì `/api/` trả 502, `/healthz` vẫn 200.
- `/assets/` (tên file có hash) → `Cache-Control: public, max-age=31536000, immutable`; thiếu file
  thì 404, không trả `index.html`.
- Còn lại → file tĩnh nếu có, không thì `index.html` (router phía trình duyệt), `Cache-Control:
  no-cache` để bản build mới có hiệu lực ngay.
- Nén gzip HTML, JS, CSS, JSON (kể cả JSON của API), SVG. Header bảo mật trên mọi phản hồi:
  `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`,
  `X-Frame-Options: DENY`.

Trong compose, `web` là cửa vào duy nhất của trình duyệt (cổng 8080 của container mở ra
`127.0.0.1` trên host); `api` không cần mở ra host. Có reverse proxy khác đứng trước `web` thì mọi
người dùng chung IP của proxy đó với API (chung bộ đếm đăng nhập sai).
