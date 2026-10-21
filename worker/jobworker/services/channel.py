"""Kênh gRPC tới gateway (TLS, keepalive, cỡ message) và cách worker xử lý từng nhóm mã lỗi gRPC."""

from __future__ import annotations

from typing import Any

import grpc

from jobworker import __version__
from jobworker.common.config import WorkerSettings

MAX_MESSAGE_BYTES = 64 * 1024 * 1024

# Gateway không biết worker này (khởi động lại, đã prune), hoặc token còn hạn nhưng đang gắn với
# worker_id khác (PERMISSION_DENIED: process khác dùng chung WORKER_TOKEN_FILE vừa Register bằng
# nó): Register lại, giữ token (gateway gắn lại token với worker này).
REREGISTER_CODES = frozenset(
    {
        grpc.StatusCode.NOT_FOUND,
        grpc.StatusCode.FAILED_PRECONDITION,
        grpc.StatusCode.PERMISSION_DENIED,
    }
)
# Khoá / token bị từ chối (sai, hết hạn): bỏ token, thử khoá kế tiếp, hết khoá thì chờ rồi thử lại.
AUTH_CODES = frozenset({grpc.StatusCode.UNAUTHENTICATED})
# Lỗi tạm thời: thử lại lời gọi. Không có RESOURCE_EXHAUSTED: message vượt trần thì gửi lại vẫn
# hỏng.
RETRY_CODES = frozenset(
    {
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.DEADLINE_EXCEEDED,
        grpc.StatusCode.ABORTED,
    }
)


def channel_options() -> list[tuple[str, Any]]:
    return [
        ("grpc.max_send_message_length", MAX_MESSAGE_BYTES),
        ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
        # Ping chỉ khi có lời gọi (Lease long-poll gần như luôn mở): server mặc định chặn client
        # ping dày khi không có lời gọi.
        ("grpc.keepalive_time_ms", 30_000),
        ("grpc.keepalive_timeout_ms", 10_000),
        ("grpc.keepalive_permit_without_calls", 0),
        ("grpc.http2.max_pings_without_data", 0),
        ("grpc.initial_reconnect_backoff_ms", 500),
        ("grpc.min_reconnect_backoff_ms", 500),
        ("grpc.max_reconnect_backoff_ms", 30_000),
        ("grpc.primary_user_agent", f"jobworker/{__version__}"),
    ]


def open_channel(settings: WorkerSettings) -> grpc.aio.Channel:
    """Kênh tới gateway: TLS khi GATEWAY_TLS. Kiểm chứng chỉ bằng GATEWAY_CA_CERT nếu có, không thì
    bằng bộ CA công khai đóng kèm grpcio (`grpc/_cython/_credentials/roots.pem`) — không phải kho
    CA của hệ điều hành."""
    if settings.gateway_tls:
        root = settings.gateway_ca_cert.read_bytes() if settings.gateway_ca_cert else None
        creds = grpc.ssl_channel_credentials(root_certificates=root)
        return grpc.aio.secure_channel(settings.gateway_addr, creds, options=channel_options())
    return grpc.aio.insecure_channel(settings.gateway_addr, options=channel_options())
