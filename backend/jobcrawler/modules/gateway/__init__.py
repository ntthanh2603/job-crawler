"""Gateway gRPC cho worker lấy trang và phía backend gửi việc lấy trang qua Redis Streams.

- `registry_service`: worker đang kết nối (Redis), dùng chung cho gateway / backend / giao diện.
- `dispatch_service`: hàng việc trên Redis Streams; `FetchClient` (backend gửi việc, chờ kết quả) và
  phần gateway dùng (giao việc, trả kết quả).
- `remote_service`: `RemoteTransport` / `RemoteRenderer` cho `HttpClient` trên `FetchClient`.
- `server_service`: servicer `CrawlGateway`, token, `GatewayServer` (`jobcrawler gateway`).
- `worker_auth_service`: khoá tham gia và token riêng của từng worker.
- `gateway_router`, `gateway_schema`: API trang Workers (`/api/workers…`).
"""
