# syntax=docker/dockerfile:1

# Backend (gói jobcrawler): API, gateway gRPC, runner, scheduler, migrate — cùng một image, khác
# lệnh chạy. Không có Chromium / curl_cffi: mọi request tới website đi qua worker. Giao diện React
# là image riêng (frontend/, nginx).
# Build context là thư mục backend/, cộng build context tên `proto` (thư viện hợp đồng gRPC):
#   docker build --build-context proto=../proto -t jobcrawler-backend .        # trong backend/
#   compose: build: {context: ./backend, additional_contexts: {proto: ./proto}}
#
# Ba giai đoạn: `deps` dựng venv dependency ở /opt/venv bằng uv; `build` cài thêm jobcrawler + proto
# lên đó và gom riêng các file vừa cài vào /layer; image chạy chép venv dependency (lớp lớn, chỉ đổi
# khi uv.lock đổi) rồi /layer (lớp vài MB, đổi mỗi lần sửa code) — sửa code thì CI chỉ đẩy và máy
# chạy chỉ tải lớp nhỏ. Không có uv, mã nguồn, cache. Cùng base image với worker/Dockerfile để hai
# image dùng chung lớp base.
ARG PYTHON_IMAGE=python:3.12-slim-bookworm

# --- Dependency (chưa có jobcrawler) -------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS deps

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=python3.12 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# Bố cục như trong repo để nguồn đường dẫn `../proto` của pyproject.toml trỏ đúng.
WORKDIR /src/backend

# Chỉ cần uv.lock, pyproject.toml và proto/ (gói cục bộ chưa cài ở bước này): layer được cache khi
# chỉ sửa code. Bỏ bộ test đi kèm numpy / scipy (dependency của datasketch, ~47 MB; không import lúc
# chạy).
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,from=proto,target=/src/proto \
    uv sync --frozen --no-dev --no-install-project --no-install-local \
    && find /opt/venv/lib/python3.12/site-packages/numpy /opt/venv/lib/python3.12/site-packages/scipy \
        -depth -type d -name tests -exec rm -rf {} +

# --- Build: jobcrawler + proto, gom vào /layer ---------------------------------------------------
FROM deps AS build

SHELL ["/bin/bash", "-o", "pipefail", "-c"]
COPY pyproject.toml uv.lock README.md ./
COPY jobcrawler jobcrawler
COPY --from=proto pyproject.toml /src/proto/
COPY --from=proto jcproto /src/proto/jcproto
# Gói cục bộ luôn cài lại: cache của uv cho gói cục bộ chỉ theo pyproject.toml, không theo mã
# nguồn, nên không ép thì image có thể mang mã cũ. Cài vào /opt/venv (không editable). File mới /
# đổi so với venv dependency (so tổng kiểm, không theo mtime: uv giữ mtime gốc khi chép) chép sang
# /layer/opt/venv; cài lại mà xoá file của venv dependency thì dừng build (lớp chồng lên không xoá
# được).
RUN --mount=type=cache,target=/root/.cache/uv \
    (cd /opt/venv && find . -type f -print0 | xargs -0 sha1sum | sort) > /tmp/before \
    && uv sync --frozen --no-dev --no-editable \
        --reinstall-package jobcrawler --reinstall-package jobcrawler-proto \
    && (cd /opt/venv && find . -type f -print0 | xargs -0 sha1sum | sort) > /tmp/after \
    && gone="$(comm -23 <(cut -c43- /tmp/before | sort) <(cut -c43- /tmp/after | sort))" \
    && { [ -z "$gone" ] || { echo "file bị xoá khỏi venv dependency: $gone" >&2; exit 1; }; } \
    && mkdir -p /layer/opt/venv \
    && comm -13 /tmp/before /tmp/after | cut -c43- \
        | (cd /opt/venv && xargs -d '\n' -r cp -a --parents -t /layer/opt/venv)

# --- Runtime -------------------------------------------------------------------------------------
FROM ${PYTHON_IMAGE}

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

RUN useradd --create-home --uid 1000 app
# venv trỏ về python của base image (/usr/local/bin/python3.12): mọi giai đoạn cùng PYTHON_IMAGE.
# Dependency trước (lớp lớn, giữ nguyên khi chỉ sửa code), mã sau.
COPY --from=deps /opt/venv /opt/venv
COPY --from=build /layer/ /

USER app
WORKDIR /home/app
EXPOSE 8000 50051
CMD ["jobcrawler", "--help"]
