# Nguồn dữ liệu

Mỗi nguồn một dòng (xem `docs/BUILD_PROMPT.md` mục 7 và 14). Trước khi viết adapter: mở trang
thật (`robots.txt`, sitemap, JSON-LD, request JSON của frontend), lưu 2–3 mẫu vào
`tests/fixtures/<source>/`, viết test parser rồi mới viết adapter.

| Nguồn | Loại | Phương thức | Rate limit | Trạng thái | Ghi chú |
|---|---|---|---|---|---|

Chưa có nguồn nào (bắt đầu từ M2: ATS + trang careers; M4: trang tuyển dụng).
