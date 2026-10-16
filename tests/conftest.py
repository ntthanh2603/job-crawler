from __future__ import annotations

import pytest

from jobcrawler.config import Settings


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Xoá biến môi trường trùng tên field của Settings để test không phụ thuộc máy."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
