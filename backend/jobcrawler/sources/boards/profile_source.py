"""Tin của một doanh nghiệp trên một trang tuyển dụng (profile đã map), cho `process_company`.

Index của profile còn mới (≤ BOARD_STALE_HOURS, mọi tin của profile đã tải) thì lấy tin từ
`job_postings`. Cũ hơn thì đọc danh sách tin trên trang profile (đủ các trang) và gộp với index:
tin đã index dùng luôn, chỉ tải trang chi tiết của tin chưa có. Đọc danh sách bị chặn/lỗi thì
dùng tạm tin đã index rồi báo nguồn chưa xong.

`network=False` (lượt poll ban ngày): không bao giờ gọi trang tuyển dụng, kể cả khi index cũ —
chỉ dùng tin đã index (lượt index đuôi giữ phần tin mới). Poll từng doanh nghiệp × từng trang sẽ
làm trang chặn ta ở quy mô 10.000 doanh nghiệp.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Protocol

from jobcrawler.logging import get_logger
from jobcrawler.schemas import RawJob
from jobcrawler.sources.base import (
    Source,
    SourceError,
    SourceSession,
    gather_all,
    transient_failure,
)
from jobcrawler.sources.boards.base import BoardAdapter
from jobcrawler.text import normalize_company_name

log = get_logger(__name__)


class ProfileIndex(Protocol):
    """Tin trên trang tuyển dụng đã có trong index (`job_postings`)."""

    async def for_profile(self) -> list[RawJob]:
        """Tin đang mở gắn với profile."""
        ...

    async def by_ids(self, external_ids: Sequence[str]) -> dict[str, RawJob]:
        """Tin chưa gỡ theo mã tin, chưa gắn profile nào hoặc gắn đúng profile này."""
        ...


class BoardProfileSource(Source):
    kind = "board"

    def __init__(
        self,
        adapter: BoardAdapter,
        *,
        external_id: str,
        url: str,
        fresh: bool,
        index: ProfileIndex,
        name: str | None = None,
        network: bool = True,
    ) -> None:
        super().__init__(external_id, url)
        self.adapter = adapter
        self.fresh = fresh
        self.index = index
        self.profile_name = normalize_company_name(name) if name else None
        self.network = network  # False: chỉ dùng index, không gọi trang tuyển dụng

    @property
    def name(self) -> str:
        return f"board:{self.adapter.name}:{self.key}"

    async def _detail(self, session: SourceSession, url: str) -> RawJob | None:
        result = await session.fetch(url, rate=self.adapter.rate)
        if not result.ok:
            if transient_failure(result):
                session.truncate("detail_error")
            return None
        parsed = self.adapter.parse_job(result.text, url)
        if parsed is None:
            session.fail_page(result, "no_job: không bóc được tin từ trang chi tiết")
            return None
        profile = parsed.profile
        if (
            profile is not None
            and profile.external_id != self.key
            and self.profile_name
            and normalize_company_name(profile.name) != self.profile_name
        ):
            # Link trong khối "việc làm tương tự" của trang profile: tin của nhà tuyển dụng khác.
            log.info("board.profile_foreign_job", url=url, employer=profile.name)
            return None
        session.count(url, 1)
        return parsed.job

    async def jobs(self, session: SourceSession) -> AsyncIterator[RawJob]:
        indexed = {job.external_id: job for job in await self.index.for_profile()}
        if self.fresh or not self.network or not self.adapter.profile_lists_jobs:
            for job in indexed.values():
                yield job
            return
        try:
            listing = await self.adapter.profile_listing(
                session, self.url, max_pages=session.limits.max_listing_pages
            )
        except SourceError:
            # Không đọc được danh sách: dùng tin đã index (có thể cũ) rồi báo lỗi nguồn.
            for job in indexed.values():
                yield job
            raise
        listed: dict[str, str] = {}
        for url in listing.urls:
            if (job_id := self.adapter.job_id(url)) is not None:
                listed.setdefault(job_id, url)
        if not listed:
            # Trang không liệt kê tin nào (hết tin, hoặc đổi bố cục): không đủ để bỏ tin index.
            for job in indexed.values():
                yield job
            return
        for job_id, job in indexed.items():
            # Danh sách chắc chắn đủ: tin index không còn trên trang là đã đóng.
            if job_id in listed or not listing.complete:
                yield job
        missing = [job_id for job_id in listed if job_id not in indexed]
        if missing:
            known = await self.index.by_ids(missing)
            for job_id in missing:
                if (job := known.get(job_id)) is not None:
                    yield job
            missing = [job_id for job_id in missing if job_id not in known]
        if len(missing) > session.limits.max_detail_pages:
            session.truncate("detail_pages")
            missing = missing[: session.limits.max_detail_pages]
        step = session.limits.detail_concurrency
        for i in range(0, len(missing), step):
            batch = [listed[job_id] for job_id in missing[i : i + step]]
            for job in await gather_all(self._detail(session, url) for url in batch):
                if job is not None:
                    yield job
