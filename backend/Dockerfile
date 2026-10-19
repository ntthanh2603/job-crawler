# syntax=docker/dockerfile:1

# Backend (gói jobcrawler): API + giao diện, gateway gRPC, runner, scheduler, migrate — cùng một
# image, khác lệnh chạy. Không có Chromium / curl_cffi: mọi request tới website đi qua worker.
# Build từ THƯ MỤC GỐC repo (cần uv.lock, proto/, frontend/):
#   docker build -f backend/Dockerfile -t jobcrawler-backend .

# --- Giao diện React (frontend/) -------------------------------------------------------------------
FROM node:22-slim AS ui
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build -- --outDir /ui/dist

# --- Ứng dụng Python -------------------------------------------------------------------------------
FROM python:3.12-slim-bookworm

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
# workspace (kể cả worker) nhưng không cần mã nguồn của chúng.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=backend/pyproject.toml,target=backend/pyproject.toml \
    --mount=type=bind,source=proto/pyproject.toml,target=proto/pyproject.toml \
    --mount=type=bind,source=worker/pyproject.toml,target=worker/pyproject.toml \
    uv sync --frozen --no-dev --package jobcrawler --no-install-workspace

COPY pyproject.toml uv.lock README.md ./
COPY worker/pyproject.toml worker/
COPY proto/pyproject.toml proto/
COPY proto/jcproto proto/jcproto
COPY backend/pyproject.toml backend/
COPY backend/jobcrawler backend/jobcrawler
# Gói của workspace luôn cài lại: cache của uv cho gói cục bộ chỉ theo pyproject.toml, không theo
# mã nguồn, nên không ép thì image có thể mang mã cũ.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package jobcrawler --no-editable \
        --reinstall-package jobcrawler --reinstall-package jobcrawler-proto

# Bản build giao diện cho `jobcrawler ui` (service api).
COPY --from=ui /ui/dist /app/ui
ENV UI_DIST=/app/ui

RUN useradd --create-home --uid 1000 app
USER app
EXPOSE 8000 50051
CMD ["jobcrawler", "--help"]
