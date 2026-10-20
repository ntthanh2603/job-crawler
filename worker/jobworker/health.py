"""File trạng thái cho healthcheck: worker ghi, `jobworker health` đọc (xem worker/README.md).

Worker ghi một file JSON nhỏ (`WORKER_HEALTH_FILE`) — ghi file tạm rồi `os.replace` nên người đọc
không bao giờ thấy file dở:

- sau mỗi lần nói chuyện được với gateway (Register / Lease / Heartbeat / Complete);
- khi lỗi kết nối gateway (Lease / Register / Heartbeat lỗi mạng, UNAVAILABLE, sai token...);
- ở mỗi nhịp heartbeat, kể cả lúc gateway mất: file luôn mới nếu event loop còn chạy.

Đổi trạng thái thì ghi ngay, không đổi thì tối đa một lần mỗi `WRITE_INTERVAL_SECONDS` (Lease /
Complete có thể vài chục lần mỗi giây).

Trạng thái (`state`):
- `starting`: chưa Register được lần nào.
- `running`: lời gọi gateway gần nhất thành công.
- `reconnecting`: đã từng kết nối, lời gọi gần nhất lỗi; worker đang thử lại.
- `draining`: thôi xin việc, làm nốt việc đang chạy rồi thoát (SIGTERM hoặc gateway yêu cầu drain).

`check` (lệnh `jobworker health`) báo không khoẻ khi: không có file / file hỏng; file cũ hơn
`max_age` (mặc định max(30 s, 3 × chu kỳ heartbeat): event loop treo hoặc process đã chết); hoặc
`starting` / `reconnecting` liên tục lâu hơn `gateway_grace` (mặc định 5 phút). Mất gateway không
làm worker không khoẻ ngay: gateway khởi động lại (vài giây tới vài chục giây) không được kéo theo
khởi động lại mọi worker; quá 5 phút thì khởi động lại worker cũng không hại gì và gỡ được lỗi nằm
ở phía worker (DNS, kênh gRPC kẹt).

Module chỉ dùng thư viện chuẩn để lệnh healthcheck khởi động nhanh.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any

STARTING = "starting"
RUNNING = "running"
RECONNECTING = "reconnecting"
DRAINING = "draining"

# Trạng thái không đổi thì ghi lại file tối đa chừng này giây một lần.
WRITE_INTERVAL_SECONDS = 1.0
# `jobworker health` mặc định: file cũ hơn max(30 s, 3 × chu kỳ heartbeat) là worker treo.
MIN_MAX_AGE_SECONDS = 30.0
MAX_AGE_HEARTBEATS = 3
# Không kết nối được gateway liên tục lâu hơn chừng này giây thì mới tính là không khoẻ.
GATEWAY_GRACE_SECONDS = 300.0


def default_max_age(heartbeat_seconds: float) -> float:
    return max(MIN_MAX_AGE_SECONDS, MAX_AGE_HEARTBEATS * heartbeat_seconds)


class HealthStatus:
    """Trạng thái kết nối gateway của worker, ghi ra `path`. Chỉ gọi từ event loop."""

    def __init__(self, path: Path, worker_id: str, *, heartbeat_seconds: float) -> None:
        self.path = path
        self.worker_id = worker_id
        self.heartbeat_seconds = heartbeat_seconds  # worker cập nhật khi gateway đổi chu kỳ
        self.last_ok_at: float | None = None
        # Chưa kết nối: tính từ lúc khởi động, để gateway chết từ trước vẫn có hạn `gateway_grace`.
        self.disconnected_since: float | None = time.time()
        self.draining = False
        self._written_state: str | None = None
        self._written_at = 0.0  # monotonic
        self._dir_ready = False
        self._failing = False

    @property
    def state(self) -> str:
        if self.draining:
            return DRAINING
        if self.disconnected_since is None:
            return RUNNING
        return RECONNECTING if self.last_ok_at is not None else STARTING

    def ok(self) -> None:
        """Một lời gọi gateway vừa thành công."""
        self.last_ok_at = time.time()
        self.disconnected_since = None
        self.touch()

    def failed(self) -> None:
        """Một lời gọi gateway vừa lỗi kết nối / xác thực; giữ mốc lỗi đầu tiên của đợt."""
        if self.disconnected_since is None:
            self.disconnected_since = time.time()
        self.touch()

    def drain(self) -> None:
        self.draining = True
        self.touch()

    def touch(self) -> None:
        """Ghi lại file khi đổi trạng thái hoặc lần ghi trước đã quá `WRITE_INTERVAL_SECONDS`."""
        fresh = time.monotonic() - self._written_at < WRITE_INTERVAL_SECONDS
        if self.state == self._written_state and fresh:
            return
        self.write()

    def snapshot(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "pid": os.getpid(),
            "state": self.state,
            "updated_at": time.time(),
            "last_ok_at": self.last_ok_at,
            "disconnected_since": self.disconnected_since,
            "heartbeat_seconds": self.heartbeat_seconds,
        }

    def write(self) -> None:
        """Ghi nguyên tử (file tạm cạnh file đích rồi `os.replace`). Lỗi ghi (ổ đầy, thư mục chỉ
        đọc) không làm worker dừng: cảnh báo một lần mỗi đợt lỗi, healthcheck sẽ báo file cũ."""
        snapshot = self.snapshot()
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            if not self._dir_ready:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._dir_ready = True
            tmp.write_text(json.dumps(snapshot), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            if not self._failing:
                # Import muộn: `jobworker health` chỉ đọc file, không cần nạp structlog.
                from jobworker.logging import get_logger

                get_logger(__name__).warning(
                    "worker.health_write_failed", file=str(self.path), error=str(exc)
                )
            self._failing = True
        else:
            self._failing = False
        self._written_state = snapshot["state"]
        self._written_at = time.monotonic()

    def remove(self) -> None:
        """Worker dừng hẳn: xoá file để healthcheck báo "không có file" thay vì "file cũ"."""
        with contextlib.suppress(OSError):
            self.path.unlink(missing_ok=True)


def check(
    path: Path,
    *,
    max_age: float | None = None,
    gateway_grace: float = GATEWAY_GRACE_SECONDS,
    now: float | None = None,
) -> tuple[bool, str]:
    """(khoẻ hay không, lý do ngắn). `max_age` None = `default_max_age` theo chu kỳ heartbeat
    ghi trong file; `gateway_grace` 0 = không xét kết nối gateway."""
    now = time.time() if now is None else now
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False, f"không có file trạng thái {path} (worker chưa chạy hoặc đã dừng)"
    except (OSError, ValueError) as exc:
        return False, f"không đọc được file trạng thái {path}: {exc}"
    try:
        updated_at = float(data["updated_at"])
        state = str(data.get("state") or "?")
        heartbeat = float(data.get("heartbeat_seconds") or 0)
        since = data.get("disconnected_since")
        since = None if since is None else float(since)
        worker_id = str(data.get("worker_id") or "?")
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        return False, f"file trạng thái {path} sai định dạng: {exc!r}"

    limit = max_age if max_age is not None else default_max_age(heartbeat)
    age = now - updated_at
    if age > limit:
        return False, (
            f"{worker_id}: trạng thái không cập nhật {age:.0f} s (> {limit:.0f} s),"
            " worker treo hoặc đã dừng"
        )
    if gateway_grace > 0 and state in (STARTING, RECONNECTING) and since is not None:
        down = now - since
        if down > gateway_grace:
            return False, (
                f"{worker_id}: không kết nối được gateway {down:.0f} s (> {gateway_grace:.0f} s)"
            )
    return True, f"{worker_id}: {state}, cập nhật {max(age, 0):.0f} s trước"
