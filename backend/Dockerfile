# syntax=docker/dockerfile:1

# Backend (gói jobcrawler): API, gateway gRPC, runner, scheduler, migrate — cùng một image, khác
# lệnh chạy. Không có Chromium / curl_cffi: mọi request tới website đi qua worker. Giao diện React
# là image riêng (frontend/, nginx).
# Build context là thư mục backend/, cộng build context tên `proto` (thư viện hợp đồng gRPC):
#   docker build --build-context proto=../proto -t jobcrawler-backend .        # trong backend/
#   compose: build: {context: ./backend, additional_contexts: {proto: ./proto}}

FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=python3.12 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

# Bố cục như trong repo để nguồn đường dẫn `../proto` của pyproject.toml trỏ đúng.
WORKDIR /src/backend

# Dependency trước để layer được cache khi chỉ sửa code: cần uv.lock, pyproject.toml và proto/
# (gói cục bộ chưa cài ở bước này).
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,from=proto,target=/src/proto \
    uv sync --frozen --no-dev --no-install-project --no-install-local

COPY pyproject.toml uv.lock README.md ./
COPY jobcrawler jobcrawler
COPY --from=proto pyproject.toml /src/proto/
COPY --from=proto jcproto /src/proto/jcproto
# Gói cục bộ luôn cài lại: cache của uv cho gói cục bộ chỉ theo pyproject.toml, không theo mã
# nguồn, nên không ép thì image có thể mang mã cũ. Cài xong (vào /opt/venv, không editable) thì bỏ
# mã nguồn: WORKDIR là /src/backend, `python -c` / `python -m` ở đây sẽ import nhầm bản chưa biên
# dịch thay vì gói đã cài.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable \
        --reinstall-package jobcrawler --reinstall-package jobcrawler-proto \
    && rm -rf jobcrawler /src/proto/jcproto

RUN useradd --create-home --uid 1000 app
USER app
EXPOSE 8000 50051
CMD ["jobcrawler", "--help"]
