# job-crawler

Thu thập tin tuyển dụng đang mở cho ~10.000 doanh nghiệp (chủ yếu tại Việt Nam) từ trang careers,
ATS và các trang tuyển dụng; xác minh, chuẩn hoá, gộp trùng, lưu PostgreSQL.

Đặc tả và milestone: [docs/BUILD_PROMPT.md](docs/BUILD_PROMPT.md). Tiến độ từng milestone ghi
trong commit message.

## Chạy nhanh

Cần [uv](https://docs.astral.sh/uv/) và Docker.

```bash
cp .env.example .env                              # điền LLM_*, SERP_* khi có
cp config/proxies.example.txt config/proxies.txt  # điền proxy; không có thì đi thẳng
uv sync
docker compose up -d          # postgres, redis, migrate, worker ×3
uv run jobcrawler db upgrade  # migrate cũng tự chạy trong compose
uv run jobcrawler run "Test"
```

Postgres và Redis mở ra host ở cổng 15432 và 16379 (đổi bằng `POSTGRES_PORT`, `REDIS_PORT`).
Worker đọc proxy từ `./config` (mount read-only vào container).

Không dùng proxy: đặt `PROXY_ENABLED=false` trong `.env` (áp dụng cho cả worker), hoặc thêm
`--no-proxy` cho một lệnh CLI, ví dụ `uv run jobcrawler --no-proxy run "Test"`.

## Job từ website doanh nghiệp

```bash
uv run jobcrawler site base.vn                 # tìm trang careers/ATS rồi liệt kê job
uv run jobcrawler site momo.vn --discover-only --steps
uv run jobcrawler site https://jobs.lever.co/acme --json
uv run jobcrawler run "Base" --website base.vn # chạy pipeline, ghi company_runs + source_fetches
```

Tìm nguồn: link trên trang chủ → sitemap.xml → đường dẫn phổ biến → subdomain careers./tuyendung.;
nhiều ứng viên thì LLM chọn trong danh sách. Kết quả cache 7 ngày trên Redis (`--refresh` bỏ qua).
ATS có adapter: Greenhouse, Lever, Ashby, Workable, Workday, BambooHR, Recruitee, Teamtailor
(SmartRecruiters chỉ nhận diện: robots.txt cấm API). Trang careers khác: JSON-LD → parser chung →
LLM; mở bằng Chromium khi HTML không có danh sách tin.

## Kiểm tra hạ tầng

```bash
uv run jobcrawler proxies check     # gọi thử qua từng proxy, cập nhật trạng thái pool
uv run jobcrawler llm check         # gọi thử LLM với structured output
uv run jobcrawler fetch https://example.com/ [--render]   # fetch qua toàn bộ tầng mạng
```

## Phát triển

```bash
uv run ruff format . && uv run ruff check .
uv run pyright
uv run pytest                 # test tích hợp cần TEST_DATABASE_URL (có sẵn trong .env.example)
uv run jobcrawler db revision -m "mô tả"   # sinh migration mới từ model
```

Test không gọi mạng ngoài: nguồn dùng `jobcrawler.fakes.FakeFetcher`, LLM dùng `FakeChatClient`.
