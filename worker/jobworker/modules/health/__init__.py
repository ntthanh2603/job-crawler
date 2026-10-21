"""Sức khoẻ của worker.

- `status_service`: file trạng thái worker ghi, `jobworker health` (healthcheck của Docker) đọc.
- `probe_service`: `jobworker check` — kết nối tới gateway và IP đi ra qua từng proxy tại worker.
"""
