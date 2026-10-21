"""Chạy một việc lấy trang (`FetchTask` → `FetchResult`).

- `executor_service`: `TaskExecutor` — kiểm việc, chọn proxy, gọi HTTP / Chromium, báo sức khoẻ
  proxy.
- `http_service`: `HttpFetcher` — một request qua curl_cffi.
- `browser_service`: `BrowserManager` — mở trang bằng Chromium (Playwright).
- `render_service`: `RenderGate` — tắt hẳn / tạm tắt render khi không mở được Chromium.
- `results_service`: dựng `FetchResult` (hạn chờ, lỗi, nén body) dùng chung cho HTTP và render.
"""
