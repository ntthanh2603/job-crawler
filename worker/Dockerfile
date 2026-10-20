# syntax=docker/dockerfile:1

# Worker lấy trang (gói jobworker). Build context là worker/, cộng build context tên `proto` trỏ
# tới proto/ (hợp đồng gRPC, dependency dạng đường dẫn ../proto):
#   docker build --build-context proto=../proto -t jobcrawler-worker .     (chạy trong worker/)
# Compose: `build: {context: ./worker, additional_contexts: {proto: ./proto}}`.
# Tag image phải khớp `playwright==1.63.0` trong pyproject.toml (Chromium có sẵn ở /ms-playwright).
# Chạy kèm `--init` (compose: `init: true`) để dọn process con của Chromium.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=python3.12 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# Bố cục giống repo (/src/worker cạnh /src/proto) vì uv.lock ghi proto là `../proto`.
WORKDIR /src/worker

# Dependency trước để layer được cache khi chỉ sửa code. Bỏ qua chính jobworker và gói cục bộ
# (proto) ở bước này: chỉ cần pyproject.toml + uv.lock, proto mount vào để uv đọc metadata.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,from=proto,target=/src/proto \
    uv sync --frozen --no-dev --no-install-project --no-install-local

COPY --from=proto pyproject.toml /src/proto/
COPY --from=proto jcproto /src/proto/jcproto
COPY pyproject.toml uv.lock ./
COPY jobworker jobworker
# Gói cục bộ luôn cài lại: cache của uv cho gói cục bộ chỉ theo pyproject.toml, không theo mã
# nguồn, nên không ép thì image có thể mang mã cũ.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable \
        --reinstall-package jobworker --reinstall-package jobcrawler-proto

USER pwuser
# Worker tự xử lý SIGTERM: thôi nhận việc, làm nốt (WORKER_SHUTDOWN_GRACE_SECONDS), Leave.
STOPSIGNAL SIGTERM
CMD ["jobworker", "run"]
