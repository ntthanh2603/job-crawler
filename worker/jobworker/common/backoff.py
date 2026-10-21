"""Backoff mũ có jitter cho lời gọi gateway lỗi."""

from __future__ import annotations

import random

BACKOFF_BASE_SECONDS = 0.5
BACKOFF_CAP_SECONDS = 30.0


class Backoff:
    """0.5 s, 1 s, 2 s... nhân ngẫu nhiên 1–1.5 để các worker không dồn cùng lúc; tối đa 30 s (ở
    mức trần vẫn ngẫu nhiên: 20–30 s)."""

    def __init__(
        self, base: float = BACKOFF_BASE_SECONDS, cap: float = BACKOFF_CAP_SECONDS
    ) -> None:
        self.base = base
        self.cap = cap
        self.attempt = 0

    def next(self) -> float:
        # Chặn trần trước rồi mới nhân jitter: chặn sau thì mọi lần ở mức trần đều đúng 30 s, các
        # worker mất gateway cùng lúc thử lại cùng nhịp.
        delay = min(self.cap / 1.5, self.base * 2**self.attempt) * random.uniform(1.0, 1.5)
        self.attempt = min(self.attempt + 1, 30)
        return delay

    def reset(self) -> None:
        self.attempt = 0
