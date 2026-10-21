# syntax=docker/dockerfile:1

# Backend (gói jobcrawler): API, gateway gRPC, runner, scheduler, migrate — cùng một image, khác
# lệnh chạy. Không có Chromium / curl_cffi: mọi request tới website đi qua worker. Giao diện React
# là image riêng (frontend/, nginx).
# Build context là thư mục backend/, cộng build context tên `proto` (thư viện hợp đồng gRPC):
#   docker build --build-context proto=../proto -t jobcrawler-backend .        # trong backend/
#   compose: build: {context: ./backend, additional_contexts: {proto: ./proto}}
#
# Hai giai đoạn: `build` dựng venv ở /opt/venv bằng uv; image chạy chỉ chép venv đó sang (không có
# uv, mã nguồn, cache). Cùng base image với worker/Dockerfile để hai image dùng chung lớp base.
ARG PYTHON_IMAGE=python:3.12-slim-bookworm

# --- Build ---------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS build

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=python3.12 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

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
# nguồn, nên không ép thì image có thể mang mã cũ. Cài vào /opt/venv (không editable). Bỏ bộ test
# đi kèm numpy / scipy (dependency của datasketch, ~47 MB; không import lúc chạy).
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable \
        --reinstall-package jobcrawler --reinstall-package jobcrawler-proto \
    && find /opt/venv/lib/python3.12/site-packages/numpy /opt/venv/lib/python3.12/site-packages/scipy \
        -depth -type d -name tests -exec rm -rf {} +

# --- Runtime -------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE}

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

RUN useradd --create-home --uid 1000 app
# venv trỏ về python của base image (/usr/local/bin/python3.12): hai giai đoạn cùng PYTHON_IMAGE.
COPY --from=build /opt/venv /opt/venv

USER app
WORKDIR /home/app
EXPOSE 8000 50051
CMD ["jobcrawler", "--help"]
