# syntax=docker/dockerfile:1

# Worker lấy trang (gói jobworker). Build context là worker/, cộng build context tên `proto` trỏ
# tới proto/ (hợp đồng gRPC, dependency dạng đường dẫn ../proto):
#   docker build --build-context proto=../proto -t jobcrawler-worker .     (chạy trong worker/)
# Compose: `build: {context: ./worker, additional_contexts: {proto: ./proto}}`.
# Chạy kèm `--init` (compose: `init: true`) để dọn process con của Chromium.
#
# Cùng base image với backend/Dockerfile (python slim) thay cho image Playwright chính thức: image
# đó mang cả Chromium đầy đủ, Firefox, WebKit và thư viện hệ thống của cả ba (~4 GB). Worker chỉ
# mở `chromium.launch(headless=True)` nên chỉ cần Chromium headless shell, cài bằng chính
# `playwright` trong venv (bản Chromium luôn khớp `playwright` của uv.lock).
ARG PYTHON_IMAGE=python:3.12-slim-bookworm

# --- Dependency (chưa có jobworker) -------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS deps

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=python3.12 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# Bố cục giống repo (/src/worker cạnh /src/proto) vì uv.lock ghi proto là `../proto`.
WORKDIR /src/worker

# Chỉ cần pyproject.toml + uv.lock (proto mount vào để uv đọc metadata): layer được cache khi chỉ
# sửa code.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,from=proto,target=/src/proto \
    uv sync --frozen --no-dev --no-install-project --no-install-local

# --- Build: venv đủ (jobworker + proto) -----------------------------------------------------------
FROM deps AS build

COPY --from=proto pyproject.toml /src/proto/
COPY --from=proto jcproto /src/proto/jcproto
COPY pyproject.toml uv.lock ./
COPY jobworker jobworker
# Gói cục bộ luôn cài lại: cache của uv cho gói cục bộ chỉ theo pyproject.toml, không theo mã
# nguồn, nên không ép thì image có thể mang mã cũ.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable \
        --reinstall-package jobworker --reinstall-package jobcrawler-proto

# --- Runtime -------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE}

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# Chromium headless shell + thư viện hệ thống của riêng nó (`--with-deps`, apt), chạy bằng
# `playwright` của venv dependency mount tạm (không nằm trong image): layer này chỉ build lại khi
# dependency đổi, không phải mỗi lần sửa code. Vá gói hệ điều hành trong cùng layer (Trivy ở CI chặn
# lỗ hổng đã có bản vá).
RUN --mount=type=bind,from=deps,source=/opt/venv,target=/opt/venv \
    playwright install --with-deps --only-shell chromium \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/* /tmp/* /root/.cache

# Cùng uid với user `pwuser` của image Playwright trước đây (file mount từ máy không đổi quyền).
RUN groupadd --gid 1001 pwuser && useradd --create-home --uid 1001 --gid 1001 pwuser
COPY --from=build /opt/venv /opt/venv

USER pwuser
WORKDIR /home/pwuser
# Worker tự xử lý SIGTERM: thôi nhận việc, làm nốt (WORKER_SHUTDOWN_GRACE_SECONDS), Leave.
STOPSIGNAL SIGTERM
# `jobworker health` đọc file trạng thái worker ghi (WORKER_HEALTH_FILE, mặc định
# /tmp/jobworker-health.json): file cũ (treo) hoặc mất gateway quá 5 phút → unhealthy. Compose
# `healthcheck:` ghi đè được.
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD ["jobworker", "health"]
CMD ["jobworker", "run"]
