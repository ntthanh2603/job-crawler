"""Giao diện adapter nguồn và phiên fetch có ghi nhận (`source_fetches`).

Mỗi nguồn (một bảng ATS, một trang careers) là một `Source`; `Source.jobs()` là async
generator nên khi bị huỷ ở deadline, các tin đã yield vẫn được giữ lại (kết quả `partial`).
Nguồn chỉ gọi mạng qua `SourceSession` (bọc `Fetcher`), test inject `FakeFetcher`.
"""

from __future__ import annotations

import asyncio
import email.utils
import re
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Any, ClassVar

from jobcrawler.llm import LLMClient
from jobcrawler.logging import get_logger
from jobcrawler.net.http import Fetcher, FetchResult
from jobcrawler.schemas import FetchOutcome, RawJob
from jobcrawler.text import strip_accents

if TYPE_CHECKING:
    from jobcrawler.config import Settings

log = get_logger(__name__)

# HTML trang parse lỗi lưu tối đa chừng này ký tự (đã nén zstd trong DB).
MAX_FAILED_HTML_CHARS = 2_000_000

VN_TZ = timezone(timedelta(hours=7))
_VN_HINTS = re.compile(
    r"\b(viet ?nam|vn|vnm|ho chi minh|hcm|hcmc|sai gon|saigon|ha ?noi|da nang|hai phong|"
    r"can tho|binh duong|dong nai|bac ninh|hue|nha trang|vung tau)\b"
)


class SourceError(Exception):
    """Nguồn dừng giữa chừng: bị chặn, robots cấm, hoặc trang chính không lấy được."""

    def __init__(self, outcome: FetchOutcome, message: str) -> None:
        super().__init__(message)
        self.outcome = outcome


@dataclass(slots=True)
class FetchRecord:
    """Một dòng `source_fetches` (run_id/company_id gắn lúc lưu)."""

    source: str
    url: str
    http_status: int | None
    proxy_id: str | None
    duration_ms: int
    outcome: FetchOutcome
    error: str | None = None
    items: int | None = None
    from_cache: bool = False
    rendered: bool = False
    # Trang tải được nhưng không bóc được: lý do + HTML (lưu vào `failed_pages`, không vào
    # `source_fetches`).
    failure: str | None = None
    html: str | None = None

    @classmethod
    def from_result(cls, source: str, result: FetchResult) -> FetchRecord:
        return cls(
            source=source,
            url=result.url,
            http_status=result.status,
            proxy_id=result.proxy_id,
            duration_ms=result.duration_ms,
            outcome=result.outcome,
            error=result.error,
            from_cache=result.from_cache,
            rendered=result.rendered,
        )


@dataclass(frozen=True, slots=True)
class SourceLimits:
    max_jobs: int = 500  # mỗi nguồn
    max_detail_pages: int = 80  # trang chi tiết mỗi nguồn
    max_listing_pages: int = 10  # trang danh sách (phân trang, "xem tất cả")
    max_rendered_pages: int = 10  # trang mở bằng trình duyệt mỗi nguồn
    detail_concurrency: int = 4
    ats_rate: float = 2.0  # request/giây tới API công khai của ATS
    max_failed_pages: int = 5  # trang parse lỗi lưu HTML, mỗi nguồn

    @classmethod
    def from_settings(cls, s: Settings) -> SourceLimits:
        return cls(
            max_jobs=s.source_max_jobs,
            max_detail_pages=s.source_max_detail_pages,
            max_listing_pages=s.source_max_listing_pages,
            max_rendered_pages=s.source_max_rendered_pages,
            ats_rate=s.ats_rate_per_second,
            max_failed_pages=s.failed_pages_per_run,
        )


@dataclass
class SourceSession:
    """Fetch/render nhân danh một nguồn: ghi `FetchRecord`, dừng nguồn khi bị chặn."""

    name: str
    fetcher: Fetcher
    llm: LLMClient
    limits: SourceLimits = field(default_factory=SourceLimits)
    records: list[FetchRecord] = field(default_factory=list)
    rendered_pages: int = 0
    failed_pages: int = 0

    def _record(self, result: FetchResult) -> FetchResult:
        self.records.append(FetchRecord.from_result(self.name, result))
        if result.outcome is FetchOutcome.BLOCKED:
            raise SourceError(FetchOutcome.BLOCKED, f"{result.error or 'blocked'}: {result.url}")
        return result

    async def fetch(self, url: str, **kwargs: Any) -> FetchResult:
        """Fetch qua tầng mạng. Bị chặn → `SourceError(BLOCKED)` (dừng cả nguồn)."""
        return self._record(await self.fetcher.fetch(url, source=self.name, **kwargs))

    async def fetch_ok(self, url: str, **kwargs: Any) -> FetchResult:
        """Như `fetch` nhưng mọi kết quả không OK đều dừng nguồn (dùng cho trang/API chính)."""
        result = await self.fetch(url, **kwargs)
        if not result.ok:
            raise SourceError(result.outcome, f"{result.error or result.outcome}: {url}")
        return result

    @property
    def can_render(self) -> bool:
        return self.rendered_pages < self.limits.max_rendered_pages

    async def render(self, url: str, **kwargs: Any) -> FetchResult:
        self.rendered_pages += 1
        return self._record(await self.fetcher.render(url, source=self.name, **kwargs))

    def fail_page(self, result: FetchResult, reason: str) -> None:
        """Trang tải được nhưng không bóc được: giữ HTML để lưu vào `failed_pages` (tối đa
        `limits.max_failed_pages` trang mỗi nguồn)."""
        if self.failed_pages >= self.limits.max_failed_pages:
            return
        for record in reversed(self.records):
            if record.url == result.url and record.failure is None:
                record.failure = reason[:500]
                record.html = result.text[:MAX_FAILED_HTML_CHARS]
                self.failed_pages += 1
                return

    def count(self, result: FetchResult | str, items: int) -> None:
        """Ghi số tin bóc được từ một lần fetch (cột `items`), theo kết quả hoặc URL đã gọi."""
        url = result if isinstance(result, str) else result.url
        for record in reversed(self.records):
            if record.url == url:
                record.items = items
                return


class Source(ABC):
    """Một nguồn tin của doanh nghiệp: `kind` là loại (greenhouse, careers...), `key` định danh
    trong loại đó (board token, host)."""

    kind: ClassVar[str]

    def __init__(self, key: str, url: str) -> None:
        self.key = key
        self.url = url

    @property
    def name(self) -> str:
        return f"{self.kind}:{self.key}"

    @abstractmethod
    def jobs(self, session: SourceSession) -> AsyncIterator[RawJob]:
        """Yield từng tin. Ném `SourceError` khi phải dừng nguồn."""

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name} {self.url}>"


def failed_page_rows(
    records: Iterable[FetchRecord], *, limit: int, run_id: Any = None
) -> list[dict[str, Any]]:
    """Dòng `failed_pages` từ các trang parse lỗi trong `records` (tối đa `limit`)."""
    rows = [
        {
            "url": r.url,
            "source": r.source,
            "reason": r.failure,
            "run_id": run_id,
            "http_status": r.http_status,
            "html": r.html,
        }
        for r in records
        if r.failure
    ]
    return rows[:limit]


# --- tiện ích cho adapter ------------------------------------------------------------------------


async def gather_all[T](coros: Iterable[Awaitable[T]]) -> list[T]:
    """Như `asyncio.gather` nhưng lỗi/huỷ ở một task thì huỷ các task còn lại rồi ném lỗi gốc."""
    tasks = [asyncio.ensure_future(c) for c in coros]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def parse_datetime(value: Any, *, end_of_day: bool = False) -> datetime | None:
    """Ngày giờ dạng rõ ràng → UTC: ISO 8601, "2026-10-15 09:31:36 UTC", RFC 822, epoch (giây
    hoặc ms). Chỉ có ngày → đầu ngày (hoặc cuối ngày nếu `end_of_day`) theo giờ Việt Nam.
    Dạng tương đối hay mơ hồ → None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        seconds = value / 1000 if value > 10**11 else value
        try:
            return datetime.fromtimestamp(seconds, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str) or not (text := value.strip()):
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        day = date.fromisoformat(text)
        moment = time(23, 59, 59) if end_of_day else time(0, 0)
        return datetime.combine(day, moment, VN_TZ).astimezone(UTC)
    iso = re.sub(r"\s*(UTC|GMT)$", "+00:00", text).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=VN_TZ)
    return parsed.astimezone(UTC)


def mentions_vietnam(text: str | None) -> bool:
    return bool(text) and bool(_VN_HINTS.search(strip_accents(text or "").lower()))


def vietnam_first[T](items: Iterable[T], location: Callable[[T], str | None]) -> list[T]:
    """Đưa tin ở Việt Nam lên trước (ổn định): bảng ATS toàn cầu có thể có hàng trăm tin,
    hết giới hạn/deadline thì phần còn lại là tin nước ngoài."""
    items = list(items)
    return sorted(items, key=lambda item: not mentions_vietnam(location(item)))


def text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def join_nonempty(parts: Iterable[Any], sep: str = ", ") -> str | None:
    kept = [p for p in (text_or_none(x) for x in parts) if p]
    # Bỏ phần trùng liên tiếp ("Hà Nội, Hà Nội").
    deduped = [p for i, p in enumerate(kept) if i == 0 or p != kept[i - 1]]
    return sep.join(deduped) or None
