"""Kênh gRPC tới gateway (TLS, keepalive, cỡ message) và cách worker xử lý từng nhóm mã lỗi gRPC."""

from __future__ import annotations

from typing import Any

import grpc

from jobworker import __version__
from jobworker.common.config import WorkerSettings

MAX_MESSAGE_BYTES = 64 * 1024 * 1024

# Gateway không biết worker này (khởi động lại, đã prune): Register lại.
REREGISTER_CODES = frozenset({grpc.StatusCode.NOT_FOUND, grpc.StatusCode.FAILED_PRECONDITION})
# Khoá / token bị từ chối: thử khoá kế tiếp, hết khoá thì chờ rồi thử lại.
AUTH_CODES = frozenset({grpc.StatusCode.UNAUTHENTICATED, grpc.StatusCode.PERMISSION_DENIED})
# Lỗi tạm thời: thử lại lời gọi.
RETRY_CODES = frozenset(
    {
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.DEADLINE_EXCEEDED,
        grpc.StatusCode.RESOURCE_EXHAUSTED,
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
    """Kênh tới gateway: TLS khi GATEWAY_TLS (CA riêng nếu có GATEWAY_CA_CERT)."""
    if settings.gateway_tls:
        root = settings.gateway_ca_cert.read_bytes() if settings.gateway_ca_cert else None
        creds = grpc.ssl_channel_credentials(root_certificates=root)
        return grpc.aio.secure_channel(settings.gateway_addr, creds, options=channel_options())
    return grpc.aio.insecure_channel(settings.gateway_addr, options=channel_options())
