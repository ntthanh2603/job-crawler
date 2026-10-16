"""Tham chiếu tới một bảng tin trên ATS và đặc tả nhận diện từng ATS."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from jobcrawler.sources.base import Source

# Subdomain của chính nhà cung cấp ATS, không phải của khách hàng.
RESERVED_SUBDOMAINS = frozenset(
    {"www", "app", "api", "help", "support", "blog", "status", "docs", "developers", "cdn"}
)


class AtsRef(BaseModel):
    """Một bảng tin của một doanh nghiệp trên một ATS (cache được dưới dạng JSON)."""

    kind: str  # greenhouse, lever, ...
    key: str  # định danh bảng tin trong ATS: board token, account, tenant/site...
    url: str  # trang bảng tin cho người xem
    params: dict[str, str] = Field(default_factory=dict)

    @property
    def name(self) -> str:
        return f"{self.kind}:{self.key}"


@dataclass(frozen=True, slots=True)
class AtsSpec:
    """Cách nhận diện một ATS từ URL và dựng nguồn tương ứng.

    `make_source` None nghĩa là nhận diện được nhưng chưa thu thập được (ghi lý do ở `note`).
    """

    kind: str
    patterns: tuple[re.Pattern[str], ...]
    make_ref: Callable[[re.Match[str]], AtsRef | None]
    make_source: Callable[[AtsRef], Source] | None
    note: str = ""

    def match(self, url: str) -> AtsRef | None:
        for pattern in self.patterns:
            if (m := pattern.search(url)) is not None and (ref := self.make_ref(m)) is not None:
                return ref
        return None
