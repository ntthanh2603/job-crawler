"""ID dòng: UUIDv7 (RFC 9562) — 48 bit thời gian (ms), 12 bit đếm, 62 bit ngẫu nhiên.

Tăng theo thời gian nên index B-tree ghi nối đuôi như bigint tự tăng (không phân mảnh như UUIDv4)
và `ORDER BY id` theo thứ tự tạo; không lộ số lượng dòng, sinh được ở bất kỳ process nào. Trong
một process id tăng chặt (bộ đếm trong cùng mili-giây, RFC 9562 §6.2 cách 1) — log ghi theo lô vẫn
giữ đúng thứ tự sự kiện. Phía DB có hàm cùng định dạng `uuid_v7()` (migration 0001) làm mặc định
cho insert bằng SQL thô.
"""

from __future__ import annotations

import os
import threading
import time
import uuid

# Nhỏ hơn mọi id: điểm bắt đầu khi duyệt bảng theo id (`WHERE id > :last ORDER BY id`).
NIL = uuid.UUID(int=0)

_lock = threading.Lock()
_last_ms = 0
_counter = 0


def uuid7() -> uuid.UUID:
    global _last_ms, _counter
    with _lock:
        ms = time.time_ns() // 1_000_000
        if ms > _last_ms:
            _last_ms = ms
            _counter = int.from_bytes(os.urandom(2)) & 0x7FF  # nửa dưới: còn chỗ để đếm
        else:
            _counter += 1
            if _counter > 0xFFF:  # hết 4096 id trong một ms: mượn ms kế tiếp
                _last_ms += 1
                _counter = 0
        ms, counter = _last_ms, _counter
    rand = int.from_bytes(os.urandom(8)) & ((1 << 62) - 1)
    return uuid.UUID(int=(ms << 80) | (0x7 << 76) | (counter << 64) | (0x2 << 62) | rand)
