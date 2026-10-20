from __future__ import annotations

import pytest

from jobcrawler.config import Settings


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Xoá biến môi trường trùng tên field của Settings để test không phụ thuộc máy; tắt file bí
    mật (SECRETS_DIR rỗng: không đọc /run/secrets của máy chạy test)."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setenv("SECRETS_DIR", "")


class FakeClock:
    """Đồng hồ giả: `sleep()` không chờ thật mà chỉ tăng thời gian."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()
