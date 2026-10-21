"""Cấu hình đang áp dụng của worker: của chính worker (`WorkerSettings`), kết hợp `WorkerConfig`
gateway trả về khi Register (số slot là trần, các trường khác gateway ghi đè)."""

from __future__ import annotations

from dataclasses import dataclass

from jcproto.v1 import crawl_pb2
from jobworker.common.config import WorkerSettings

DEFAULT_HEARTBEAT_SECONDS = 5.0
DEFAULT_LEASE_WAIT_MS = 1000


def _cap(remote: int, local: int) -> int:
    """Số slot gateway đẩy xuống là trần (> 0), không phải giá trị ghi đè."""
    return min(remote, local) if remote > 0 else local


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """Cấu hình đang áp dụng: của worker, kết hợp `WorkerConfig` gateway trả về."""

    http_slots: int
    render_slots: int
    impersonate: str
    max_body_bytes: int
    heartbeat_seconds: float
    lease_wait_ms: int
    use_local_proxies: bool
    version: int = 0
    # Danh sách proxy chung gateway gửi xuống (PROXY_MODE=worker) và mã băm của nó.
    shared_proxies: tuple[str, ...] = ()
    shared_hash: str = ""

    @classmethod
    def local(cls, settings: WorkerSettings) -> RuntimeConfig:
        return cls(
            http_slots=settings.http_slots,
            render_slots=settings.render_slots,
            impersonate=settings.worker_impersonate,
            max_body_bytes=settings.worker_max_body_bytes,
            heartbeat_seconds=DEFAULT_HEARTBEAT_SECONDS,
            lease_wait_ms=DEFAULT_LEASE_WAIT_MS,
            use_local_proxies=True,
        )

    def merge(self, cfg: crawl_pb2.WorkerConfig) -> RuntimeConfig:
        """`self` là cấu hình gốc của worker. Số slot của gateway là trần: min(gateway, worker), để
        máy nhỏ không bị nâng lên bằng máy lớn nhất; WORKER_RENDER_SLOTS=0 thì luôn 0. Các trường
        khác: gateway ghi đè. Trường 0 / rỗng giữ cấu hình của worker."""
        return RuntimeConfig(
            http_slots=_cap(cfg.http_slots, self.http_slots),
            render_slots=_cap(cfg.render_slots, self.render_slots),
            impersonate=cfg.impersonate or self.impersonate,
            max_body_bytes=cfg.max_body_bytes or self.max_body_bytes,
            heartbeat_seconds=float(cfg.heartbeat_seconds or self.heartbeat_seconds),
            lease_wait_ms=cfg.lease_wait_ms or self.lease_wait_ms,
            use_local_proxies=cfg.use_local_proxies,
            version=cfg.version,
            shared_proxies=tuple(cfg.shared_proxies),
            shared_hash=cfg.shared_proxies_hash,
        )
