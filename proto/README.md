# jobcrawler-proto

Hợp đồng gRPC dùng chung giữa **backend** (gateway gRPC, phía server) và **worker** lấy trang (phía
client). Đây là thư viện đứng riêng: có `pyproject.toml`, `uv.lock`, `.venv` và Taskfile của mình;
không chạy như một service.

- Gói Python: `jobcrawler-proto`, module `jcproto` — import `from jcproto.v1 import crawl_pb2,
  crawl_pb2_grpc`.
- Package protobuf: `jobcrawler.crawl.v1`, nguồn ở `jcproto/v1/crawl.proto`.
- Mã Python sinh sẵn (`crawl_pb2.py`, `crawl_pb2.pyi`, `crawl_pb2_grpc.py`) được commit cùng file
  `.proto`; lúc build image không sinh lại.

## Hợp đồng

Service `CrawlGateway` (worker gọi, gateway trong backend trả lời):

| RPC         | Ý nghĩa                                                                                  |
| ----------- | ---------------------------------------------------------------------------------------- |
| `Register`  | Worker khởi động (hoặc gateway không biết worker này): gửi `WorkerInfo`, nhận `WorkerConfig` |
| `Lease`     | Xin việc theo số slot HTTP / render còn trống, long-poll tối đa `wait_ms`; trả `FetchTask` |
| `Complete`  | Trả `FetchResult` (gộp nhiều việc một lần gọi)                                             |
| `Heartbeat` | Báo sống kèm `WorkerStats`; nhận phiên bản cấu hình và lệnh `drain`                        |
| `Leave`     | Worker dừng hẳn sau khi trả hết việc đang làm                                              |

Kiểu chính: `FetchTask` (một request HTTP qua curl_cffi hoặc một lần mở trang bằng Chromium, phân
biệt bằng `TaskKind`), `FetchResult` (phản hồi nguyên văn, lỗi truyền tải qua `ErrorKind`),
`CapturedJson`, `WorkerInfo`, `WorkerConfig`, `WorkerStats`. Chi tiết từng trường nằm ở comment
trong `crawl.proto`.

## Quy tắc phiên bản

- Chỉ **thêm** trường mới với số trường mới. Không đổi số, kiểu hay ý nghĩa của trường đã có; không
  dùng lại số của trường đã xoá (khai báo `reserved` cho số và tên đó).
- Giá trị 0 / rỗng của trường mới phải nghĩa là "không có / giữ hành vi cũ", vì bên chưa biết trường
  đó sẽ không gửi nó.
- Enum chỉ thêm giá trị mới; bên nhận phải chịu được giá trị lạ.
- Backend và gateway (cùng image backend) phát hành cùng nhau. Worker được phép chậm **một** bản:
  gateway mới phải phục vụ được worker của bản trước.
- Thay đổi không tương thích thì tạo package mới (`jcproto/v2`, `jobcrawler.crawl.v2`) và chạy song
  song với v1 cho tới khi mọi worker đã lên bản mới; không sửa v1 theo kiểu phá vỡ.

## Sửa hợp đồng

1. Sửa `jcproto/v1/crawl.proto` theo quy tắc trên.
2. Sinh lại mã: `task gen` trong `proto/`, hoặc `task proto:gen` ở gốc repo.
3. Commit file `.proto` **cùng** ba file mã sinh ra trong một commit. `task gen:check` (CI và test
   `tests/test_proto_generated.py` cũng chạy) báo lỗi nếu mã sinh không khớp `.proto`.
4. Chạy test của `backend/` và `worker/`: cả hai dùng mã mới qua path dependency.

Mã sinh ra ghi phiên bản `grpcio-tools` / protoc đã dùng và kiểm tra lúc import rằng `grpcio` /
`protobuf` lúc chạy không cũ hơn bản đó. Nâng `grpcio-tools` trong nhóm dev thì nâng cận dưới của
`grpcio` / `protobuf` trong `dependencies` cho khớp.

## Ai dùng thư viện này

- `backend/` và `worker/` khai báo dependency `jobcrawler-proto` với nguồn đường dẫn trong
  `pyproject.toml` của mình:

  ```toml
  [tool.uv.sources]
  jobcrawler-proto = { path = "../proto" }
  ```

- Dockerfile của backend và worker nhận thư mục này qua named build context `proto` (compose:
  `additional_contexts`, CLI: `docker build --build-context proto=../proto`). `.dockerignore` ở đây
  bỏ `.venv`, cache, `tests/` và `scripts/` khỏi context đó.

## Lệnh

Chạy trong `proto/` (từ gốc repo thì thêm tiền tố `proto:`, vd. `task proto:check`):

| Lệnh             | Việc                                               |
| ---------------- | -------------------------------------------------- |
| `task setup`     | `uv sync`: tạo `.venv`, cài dependency + công cụ dev |
| `task gen`       | Sinh lại mã Python từ `.proto`                     |
| `task gen:check` | Kiểm tra mã sinh sẵn đã khớp `.proto`              |
| `task fmt`       | ruff format + tự sửa lỗi lint                      |
| `task lint`      | ruff format --check + ruff check                   |
| `task typecheck` | pyright                                            |
| `task test`      | pytest                                             |
| `task check`     | lint + typecheck + gen:check + test (như CI)       |
