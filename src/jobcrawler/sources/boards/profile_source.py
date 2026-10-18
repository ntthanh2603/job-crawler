"""Tin của một doanh nghiệp trên một trang tuyển dụng (profile đã map), cho `process_company`.

Index của profile còn mới (≤ BOARD_STALE_HOURS) thì lấy tin từ `job_postings`; cũ hơn thì crawl
trực tiếp trang profile (danh sách tin → trang chi tiết). Crawl bị chặn/lỗi thì dùng tạm tin đã
index rồi báo nguồn chưa xong.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

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

log = get_logger(__name__)

LoadIndexed = Callable[[], Awaitable[list[RawJob]]]


class BoardProfileSource(Source):
    kind = "board"

    def __init__(
        self,
        adapter: BoardAdapter,
        *,
        external_id: str,
        url: str,
        fresh: bool,
        load_indexed: LoadIndexed,
    ) -> None:
        super().__init__(external_id, url)
        self.adapter = adapter
        self.fresh = fresh
        self.load_indexed = load_indexed

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
        session.count(url, 1)
        return parsed.job

    async def _crawl(self, session: SourceSession) -> AsyncIterator[RawJob]:
        jobs_url = self.adapter.profile_jobs_url(self.url)
        page = await session.fetch_ok(jobs_url, rate=self.adapter.rate)
        urls = self.adapter.profile_job_urls(page.text, page.final_url)
        session.count(jobs_url, len(urls))
        if len(urls) > session.limits.max_detail_pages:
            session.truncate("detail_pages")
            urls = urls[: session.limits.max_detail_pages]
        step = session.limits.detail_concurrency
        for i in range(0, len(urls), step):
            for job in await gather_all(self._detail(session, u) for u in urls[i : i + step]):
                if job is not None:
                    yield job

    async def jobs(self, session: SourceSession) -> AsyncIterator[RawJob]:
        if self.fresh or not self.adapter.profile_lists_jobs:
            for job in await self.load_indexed():
                yield job
            return
        yielded = 0
        try:
            async for job in self._crawl(session):
                yielded += 1
                yield job
        except SourceError:
            if yielded == 0:
                # Không crawl được: dùng tin đã index (có thể cũ) rồi báo lỗi nguồn.
                for job in await self.load_indexed():
                    yield job
            raise
