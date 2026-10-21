"""Phía worker của gateway gRPC (`jobcrawler.modules.gateway` ở backend).

- `worker_service`: `Worker` — Register → (Lease → chạy việc → Complete) song song Heartbeat →
  Leave.
- `runtime_service`: `RuntimeConfig` — cấu hình của worker kết hợp `WorkerConfig` gateway trả về.
"""
