from __future__ import annotations

import time

from jobcrawler.database.ids import NIL, uuid7


def test_uuid7_layout_and_time_order() -> None:
    before = time.time_ns() // 1_000_000
    first = uuid7()
    after = time.time_ns() // 1_000_000
    assert first.version == 7
    assert first.variant == "specified in RFC 4122"
    assert before <= first.int >> 80 <= after  # 48 bit đầu: thời gian (ms)
    time.sleep(0.002)
    assert uuid7() > first  # ms sau → lớn hơn: index ghi nối đuôi, ORDER BY id ~ thứ tự tạo
    burst = [uuid7() for _ in range(10_000)]  # nhiều id trong cùng ms: vẫn tăng chặt
    assert burst == sorted(set(burst))
    assert all(i.version == 7 for i in burst)
    assert first > NIL
