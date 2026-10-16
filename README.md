# job-crawler

Thu thập tin tuyển dụng đang mở cho ~10.000 doanh nghiệp (chủ yếu tại Việt Nam) từ trang careers,
ATS và các trang tuyển dụng; xác minh, chuẩn hoá, gộp trùng, lưu PostgreSQL.

- Đặc tả và milestone: [docs/BUILD_PROMPT.md](docs/BUILD_PROMPT.md)
- Tiến độ: [PROGRESS.md](PROGRESS.md)
- Nguồn dữ liệu: [docs/sources.md](docs/sources.md)

## Chạy nhanh

Cần [uv](https://docs.astral.sh/uv/) và Docker.

```bash
cp .env.example .env          # điền LLM_*, SERP_* khi có
uv sync
docker compose up -d          # postgres, redis, migrate, worker ×3
uv run jobcrawler db upgrade  # migrate cũng tự chạy trong compose
uv run jobcrawler run "Test"
```

Postgres và Redis mở ra host ở cổng 15432 và 16379 (đổi bằng `POSTGRES_PORT`, `REDIS_PORT`).

## Phát triển

```bash
uv run ruff format . && uv run ruff check .
uv run pyright
uv run pytest                 # test tích hợp cần TEST_DATABASE_URL (có sẵn trong .env.example)
uv run jobcrawler db revision -m "mô tả"   # sinh migration mới từ model
```
