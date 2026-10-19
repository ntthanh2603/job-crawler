# syntax=docker/dockerfile:1

# Worker lấy trang (gói jobworker). Build từ THƯ MỤC GỐC repo (cần uv.lock và proto/):
#   docker build -f worker/Dockerfile -t jobcrawler-worker .
# Tag image phải khớp `playwright==1.63.0` trong worker/pyproject.toml (Chromium có sẵn ở
# /ms-playwright). Chạy kèm `--init` (compose: `init: true`) để dọn process con của Chromium.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=python3.12 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependency trước để layer được cache khi chỉ sửa code. uv cần pyproject.toml của mọi member
# workspace (kể cả backend) nhưng không cần mã nguồn backend.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=backend/pyproject.toml,target=backend/pyproject.toml \
    --mount=type=bind,source=proto/pyproject.toml,target=proto/pyproject.toml \
    --mount=type=bind,source=worker/pyproject.toml,target=worker/pyproject.toml \
    uv sync --frozen --no-dev --package jobworker --no-install-workspace

COPY pyproject.toml uv.lock ./
COPY backend/pyproject.toml backend/
COPY proto/pyproject.toml proto/
COPY proto/jcproto proto/jcproto
COPY worker/pyproject.toml worker/
COPY worker/jobworker worker/jobworker
# Gói của workspace luôn cài lại: cache của uv cho gói cục bộ chỉ theo pyproject.toml, không theo
# mã nguồn, nên không ép thì image có thể mang mã cũ.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package jobworker --no-editable \
        --reinstall-package jobworker --reinstall-package jobcrawler-proto

USER pwuser
# Worker tự xử lý SIGTERM: thôi nhận việc, làm nốt (WORKER_SHUTDOWN_GRACE_SECONDS), Leave.
STOPSIGNAL SIGTERM
CMD ["jobworker", "run"]
