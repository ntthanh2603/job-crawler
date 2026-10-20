# syntax=docker/dockerfile:1

# Giao diện (service `frontend`): bản build Vite trên nginx không root, cổng 8080; /api/ chuyển tới
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

# API_UPSTREAM: scheme://host:port của API (không có đường dẫn). GATEWAY_UPSTREAM: gateway gRPC
# (grpc:// khi worker nối vào gateway, grpcs:// nếu có TLS). REAL_IP_FROM: CIDR của load
# balancer / proxy đứng trước (cách nhau bằng dấu cách hoặc dấu phẩy; rỗng = không dùng), xem
# nginx/18-real-ip.envsh. Entrypoint của image chạy các script trong /docker-entrypoint.d/ theo thứ
# tự tên rồi envsubst trên /etc/nginx/templates/*.template, chỉ với các biến khớp
# NGINX_ENVSUBST_FILTER; NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1 đặt NGINX_LOCAL_RESOLVERS (DNS của
# container) cho `resolver`, 18-real-ip.envsh đặt NGINX_REAL_IP từ REAL_IP_FROM.
ENV API_UPSTREAM=http://backend:8000 \
    GATEWAY_UPSTREAM=grpc://backend:50051 \
    REAL_IP_FROM="" \
    NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1 \
    NGINX_ENVSUBST_FILTER="^(API_UPSTREAM|GATEWAY_UPSTREAM|NGINX_LOCAL_RESOLVERS|NGINX_REAL_IP)\$"

COPY --chmod=0755 nginx/18-real-ip.envsh /docker-entrypoint.d/18-real-ip.envsh
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=5s --retries=5 \
    CMD wget -q -O /dev/null http://127.0.0.1:8080/healthz || exit 1
