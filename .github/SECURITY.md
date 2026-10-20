# Chính sách bảo mật

## Phiên bản được hỗ trợ

Chỉ nhánh `main` mới nhất được sửa lỗi bảo mật (không có bản phát hành / nhánh LTS). Commit cũ
hơn không được vá riêng — cập nhật lên `main`.

## Báo lỗ hổng

Báo **riêng tư** qua GitHub: tab **Security → Report a vulnerability** của repo
([tạo security advisory](https://github.com/ntthanh2603/job-crawler/security/advisories/new)).
Không đăng chi tiết khai thác trong issue công khai. Vấn đề không nhạy cảm (đề xuất gia cố, nâng
dependency, lo ngại lý thuyết không có cách khai thác) thì mở issue thường.

### Nên kèm theo

- Thành phần bị ảnh hưởng (`backend` — API / giao diện / gateway / runner, `worker`, `proto`,
  `frontend`, file compose trong `deploy/`) và commit đang chạy.
- Các bước tái hiện hoặc PoC, cấu hình liên quan (biến môi trường — **đã xoá giá trị bí mật**).
- Tác động: kẻ tấn công được gì (đọc / sửa dữ liệu, chiếm worker, lộ token / API key, SSRF qua
  worker, vượt đăng nhập giao diện…).

### Sau khi báo

Trao đổi trong advisory riêng tư; không có SLA cố định. Bản sửa được commit thẳng lên `main`, người
báo được ghi công nếu muốn.

## Phạm vi và giả định triển khai

- **Gateway gRPC** (`jobcrawler gateway`, cổng 50051): mở ra ngoài mạng nội bộ thì **bắt buộc**
  bật xác thực (`GATEWAY_TOKEN`: worker vào bằng khoá tham gia — khoá cố định hoặc khoá một lần —
  rồi dùng token riêng chỉ nằm trên Redis) và TLS — `GATEWAY_TLS_CERT/KEY` hoặc load balancer có
  TLS (HTTP/2). Chỉ health check (`grpc.health.v1`) không cần khoá.
- **Giao diện web / API**: chỉ container `frontend` (nginx) mở cổng, compose chỉ mở ở `127.0.0.1`;
  API của `backend` (`jobcrawler api`) chỉ nhận từ `frontend` trong mạng nội bộ. Mở ra ngoài thì đặt
  `UI_PASSWORD` và `JWT_SECRET` (backend/.env; trang đăng nhập, phiên JWT trong cookie httpOnly) và
  đặt `frontend` sau reverse proxy có TLS.
- **Postgres, Redis** chỉ mở ở `127.0.0.1` trên máy chạy compose (Redis không mật khẩu); ở triển
  khai nhiều máy chúng nằm trong mạng riêng, không bao giờ mở ra Internet.
- **Worker** chỉ giữ khoá tham gia / token riêng và proxy của chính nó; không có thông tin DB, Redis,
  LLM, SERP. Worker bị chiếm chỉ lộ token của nó và proxy của máy đó — thu hồi bằng cách xoá token
  trên Redis (`<prefix>:gw:wtoken:*`) và đổi `GATEWAY_TOKEN` nếu máy đó có khoá cố định.
- **Trường nhạy cảm trong DB** (khoá API LLM / SERP, proxy chung có mật khẩu) lưu mã hoá
  AES-256-GCM bằng `ENCRYPTION_KEYS`; khoá không bao giờ nằm trong Postgres.
- **API key SERP / LLM** chỉ nằm ở backend (gọi thẳng từ backend, không qua worker).
- **Bí mật không bao giờ hiện ở giao diện** và được che trong log / nhật ký thay đổi cài đặt (mật
  khẩu trong URL proxy, token, API key). Mục kết nối / bí mật hạ tầng chỉ đặt bằng biến môi trường,
  API không trả ra.
- **Bí mật nằm trong `.env` / `deploy/*.env`, không bao giờ trong git** (kể cả file proxy và chứng
  chỉ TLS). Lộ khoá thì người vận hành tự thay (đổi `GATEWAY_TOKEN`, API key, mật khẩu).
- Cấu hình sai khi triển khai (mở cổng ra Internet, bỏ token / mật khẩu) không phải lỗi mã nguồn —
  trừ khi mặc định trong mã / compose không an toàn: khi đó vẫn báo như lỗ hổng.
