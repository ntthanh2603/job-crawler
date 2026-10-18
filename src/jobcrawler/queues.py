"""Hàng đợi SAQ: tham số và kết quả job ghi JSON vào Redis; id (UUID), datetime... ghi dạng chuỗi
thay vì lỗi "not JSON serializable"."""

from __future__ import annotations

import json
from typing import Any

from saq import Queue


def _dump(value: Any) -> str:
    return json.dumps(value, default=str)


def queue_from_url(redis_url: str, name: str) -> Queue:
    return Queue.from_url(redis_url, name=name, dump=_dump)
