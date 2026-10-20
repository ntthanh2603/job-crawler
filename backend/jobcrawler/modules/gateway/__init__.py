"""Gateway gRPC cho worker lấy trang và phía backend gửi việc lấy trang qua Redis Streams.

- `registry`: worker đang kết nối (Redis), dùng chung cho gateway / backend / giao diện.
- `dispatch`: hàng việc trên Redis Streams; `FetchClient` (backend gửi việc, chờ kết quả) và phần
  gateway dùng (giao việc, trả kết quả).
- `remote`: `RemoteTransport` / `RemoteRenderer` cho `HttpClient` trên `FetchClient`.
- `server`: servicer `CrawlGateway`, token, `GatewayServer` (`jobcrawler gateway`).
- `worker_auth`: khoá tham gia và token riêng của từng worker.
- `router`, `schemas`: API trang Workers (`/api/workers…`).
"""
