# syntax=docker/dockerfile:1

# Giao diện (service `web`): bản build Vite trên nginx không root, cổng 8080; /api/ chuyển tới
# backend (API_UPSTREAM). Build context là frontend/: client API sinh sẵn (src/lib/gen/) đã commit,
# không cần file nào của backend.

# --- Build (Node theo .nvmrc) ----------------------------------------------------------------------
# Chạy trên kiến trúc của máy build: kết quả chỉ là file tĩnh, build image arm64 không phải chạy
# Node / Vite qua giả lập.
FROM --platform=$BUILDPLATFORM node:22-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY . .
RUN npm run build

# --- Runtime ---------------------------------------------------------------------------------------
# nginx bản stable, chạy bằng user 101 (không root), nghe cổng 8080.
FROM nginxinc/nginx-unprivileged:1.30-alpine

# API_UPSTREAM: scheme://host:port của API (không có đường dẫn). Entrypoint của image chạy envsubst
# trên /etc/nginx/templates/*.template, chỉ với các biến khớp NGINX_ENVSUBST_FILTER;
# NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1 đặt NGINX_LOCAL_RESOLVERS (DNS của container) cho `resolver`.
ENV API_UPSTREAM=http://api:8000 \
    NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1 \
    NGINX_ENVSUBST_FILTER="^(API_UPSTREAM|NGINX_LOCAL_RESOLVERS)\$"

COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=5s --retries=5 \
    CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
