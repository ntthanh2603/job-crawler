"""Index tăng dần một trang tuyển dụng vào `job_postings` + `source_profiles`.

Mỗi lượt:

1. Đọc danh sách tin của trang (sitemap kèm `lastmod`).
2. So với DB: tin còn thấy được đánh dấu `last_seen_at`; chỉ tải trang chi tiết của tin mới,
   tin có `lastmod` mới hơn, tin lâu chưa tải lại (`refresh_days`) hoặc tin đã gỡ nay thấy lại.
   Tải lại gửi kèm ETag / Last-Modified đã lưu: 304 = không đổi. Nội dung không đổi (cùng
   `content_hash`) thì không tính là cập nhật.
3. Upsert tin + profile nhà tuyển dụng theo lô (tiến độ không mất khi bị dừng giữa chừng).
4. Danh sách đọc đủ thì tin không còn thấy bị cộng `missed_runs`; vắng 2 lượt liên tiếp → `removed`.
5. Đánh dấu `indexed_at` cho profile đã được làm mới, rồi map profile mới ↔ doanh nghiệp.

Một trang chỉ được index bởi một process tại một thời điểm (pg advisory lock).
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, func, insert, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from jobcrawler.db.models import BoardIndexRun, JobPosting, SourceFetch, SourceProfile
from jobcrawler.llm import LLMClient
from jobcrawler.logging import get_logger
from jobcrawler.net.http import Fetcher, FetchResult
from jobcrawler.pipeline.profiles import map_profiles
from jobcrawler.schemas import JobStatus, RunStatus
from jobcrawler.sources.base import FetchRecord, SourceError, SourceLimits, SourceSession
from jobcrawler.sources.boards.base import (
    BoardAdapter,
    BoardEntry,
    BoardJob,
    ListingState,
    ProfileRef,
)
from jobcrawler.text import normalize_company_name

log = get_logger(__name__)

REMOVE_AFTER_MISSED_RUNS = 2
# Danh sách mới ít hơn chừng này phần số tin đang mở trong DB thì nghi danh sách hỏng.
MAX_DROP = 0.5
MIN_ACTIVE_FOR_DROP_CHECK = 50
_CHUNK = 5_000  # số id mỗi câu UPDATE


@dataclass(frozen=True, slots=True)
class IndexOptions:
    max_jobs: int | None = None  # số trang chi tiết tối đa mỗi lượt (phần còn lại để lượt sau)
    budget_seconds: float | None = None  # thời gian tối đa của lượt
    refresh_days: float = 7.0  # tải lại tin không đổi `lastmod` sau chừng này ngày
    concurrency: int = 4
    full: bool = False  # tải lại mọi tin, bỏ qua ETag/Last-Modified
    flush_every: int = 25
    map_profiles: bool = True
    profile_trigram: float = 0.6
    profile_min_confidence: float = 0.8
    max_profile_pages: int = 100  # trang profile đọc thêm (website/MST) khi map mỗi lượt


@dataclass(slots=True)
class IndexStats:
    source: str
    run_id: int | None = None
    status: RunStatus = RunStatus.RUNNING
    complete: bool = False  # danh sách đọc đủ
    truncated: bool = False  # còn tin chưa tải vì giới hạn số trang/thời gian
    urls_seen: int = 0
    to_fetch: int = 0
    fetched: int = 0
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    failed: int = 0
    removed: int = 0
    profiles_new: int = 0
    profiles_matched: int = 0
    duration_ms: int = 0
    error: str | None = None
    skipped: str | None = None  # không chạy (đang chạy ở process khác)
    listing_errors: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Existing:
    id: int
    lastmod: datetime | None
    fetched_at: datetime | None
    status: str
    etag: str | None
    http_last_modified: str | None
    content_hash: str | None


@dataclass(slots=True)
class _Outcome:
    entry: BoardEntry
    existing: _Existing | None
    result: FetchResult | None = None
    parsed: BoardJob | None = None
    kind: str = "failed"  # parsed | not_modified | gone | failed


def content_hash(parsed: BoardJob) -> str:
    return hashlib.sha256(parsed.job.model_dump_json().encode()).hexdigest()[:32]


def plan_fetches(
    entries: dict[str, BoardEntry],
    existing: dict[str, _Existing],
    *,
    now: datetime,
    refresh_days: float,
    full: bool,
) -> list[tuple[BoardEntry, _Existing | None]]:
    """Tin cần tải trang chi tiết, theo thứ tự ưu tiên: mới → đã đổi/thấy lại → lâu chưa tải."""
    stale_before = now - timedelta(days=refresh_days)
    new, changed, stale = [], [], []
    for external_id, entry in entries.items():
        row = existing.get(external_id)
        if row is None:
            new.append((entry, row))
        elif (
            full
            or row.fetched_at is None
            or row.status == JobStatus.REMOVED.value
            or (entry.lastmod is not None and (row.lastmod is None or entry.lastmod > row.lastmod))
        ):
            changed.append((entry, row))
        elif row.fetched_at < stale_before:
            stale.append((entry, row))
    return [*new, *changed, *stale]


class BoardIndexer:
    def __init__(
        self,
        adapter: BoardAdapter,
        *,
        engine: AsyncEngine,
        sessionmaker: async_sessionmaker[AsyncSession],
        fetcher: Fetcher,
        llm: LLMClient,
        options: IndexOptions | None = None,
        limits: SourceLimits | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        self.adapter = adapter
        self.engine = engine
        self.sessionmaker = sessionmaker
        self.options = options or IndexOptions()
        self.records: list[FetchRecord] = []
        self.session = SourceSession(
            adapter.source, fetcher, llm, limits or SourceLimits(), self.records
        )
        self.llm = llm
        self.clock = clock
        self.stats = IndexStats(source=adapter.name)
        self.profile_ids: dict[str, int] = {}
        self._profile_pages = 0

    # --- DB ---

    async def _start_run(self, started_at: datetime) -> None:
        async with self.sessionmaker.begin() as s:
            run = BoardIndexRun(source=self.adapter.name, started_at=started_at)
            s.add(run)
            await s.flush()
            self.stats.run_id = run.id

    async def _finish_run(self) -> None:
        st = self.stats
        async with self.sessionmaker.begin() as s:
            await s.execute(
                update(BoardIndexRun)
                .where(BoardIndexRun.id == st.run_id)
                .values(
                    finished_at=datetime.now(UTC),
                    status=st.status.value,
                    complete=st.complete,
                    urls_seen=st.urls_seen,
                    fetched=st.fetched,
                    new=st.new,
                    updated=st.updated,
                    unchanged=st.unchanged,
                    failed=st.failed,
                    removed=st.removed,
                    profiles_new=st.profiles_new,
                    profiles_matched=st.profiles_matched,
                    error=st.error,
                    stats={
                        "to_fetch": st.to_fetch,
                        "truncated": st.truncated,
                        "listing_errors": st.listing_errors[:20],
                        "duration_ms": st.duration_ms,
                    },
                )
            )

    async def _load_existing(self) -> dict[str, _Existing]:
        async with self.sessionmaker() as s:
            rows = await s.execute(
                select(
                    JobPosting.external_id,
                    JobPosting.id,
                    JobPosting.lastmod,
                    JobPosting.fetched_at,
                    JobPosting.status,
                    JobPosting.etag,
                    JobPosting.http_last_modified,
                    JobPosting.content_hash,
                ).where(JobPosting.source == self.adapter.name)
            )
            return {r[0]: _Existing(*r[1:]) for r in rows}

    async def _touch(self, ids: list[int], now: datetime) -> None:
        """Tin còn thấy trong danh sách: cập nhật `last_seen_at`, xoá đếm vắng mặt."""
        for i in range(0, len(ids), _CHUNK):
            async with self.sessionmaker.begin() as s:
                await s.execute(
                    update(JobPosting)
                    .where(JobPosting.id.in_(ids[i : i + _CHUNK]))
                    .values(last_seen_at=now, missed_runs=0)
                )

    async def _upsert_profiles(self, s: AsyncSession, profiles: list[ProfileRef]) -> None:
        unique = {p.external_id: p for p in profiles}
        for p in unique.values():
            stmt = pg_insert(SourceProfile).values(
                source=self.adapter.name,
                external_id=p.external_id,
                url=p.url,
                name_on_source=p.name,
                name_norm=normalize_company_name(p.name),
                website=p.website,
                tax_code=p.tax_code,
                info=p.info,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[SourceProfile.source, SourceProfile.external_id],
                set_={
                    "url": stmt.excluded.url,
                    "name_on_source": stmt.excluded.name_on_source,
                    "name_norm": stmt.excluded.name_norm,
                    "website": func.coalesce(stmt.excluded.website, SourceProfile.website),
                    "tax_code": func.coalesce(stmt.excluded.tax_code, SourceProfile.tax_code),
                    "info": SourceProfile.info.op("||")(stmt.excluded.info),
                    "updated_at": func.now(),
                },
            ).returning(SourceProfile.id, literal_column("(xmax = 0)"))
            row = (await s.execute(stmt)).one()
            self.profile_ids[p.external_id] = row[0]
            if row[1]:
                self.stats.profiles_new += 1

    async def _flush(self, outcomes: list[_Outcome], now: datetime) -> None:
        records, self.records[:] = list(self.records), []
        parsed = [o for o in outcomes if o.kind == "parsed" and o.parsed is not None]
        async with self.sessionmaker.begin() as s:
            await self._upsert_profiles(
                s, [o.parsed.profile for o in parsed if o.parsed and o.parsed.profile]
            )
            for o in parsed:
                assert o.parsed is not None
                assert o.result is not None
                job = o.parsed.job
                digest = content_hash(o.parsed)
                if o.existing is None:
                    self.stats.new += 1
                elif o.existing.content_hash == digest:
                    self.stats.unchanged += 1
                else:
                    self.stats.updated += 1
                expired = job.expires_at is not None and job.expires_at < now
                values = {
                    "url": o.entry.url,
                    "source_profile_id": self.profile_ids.get(o.parsed.profile.external_id)
                    if o.parsed.profile
                    else None,
                    "raw": job.model_dump(mode="json"),
                    "content_hash": digest,
                    "last_seen_at": now,
                    "status": (JobStatus.EXPIRED if expired else JobStatus.ACTIVE).value,
                    "lastmod": o.entry.lastmod,
                    "etag": o.result.headers.get("etag"),
                    "http_last_modified": o.result.headers.get("last-modified"),
                    "fetched_at": now,
                    "expires_at": job.expires_at,
                    "missed_runs": 0,
                }
                stmt = pg_insert(JobPosting).values(
                    source=self.adapter.name, external_id=o.entry.external_id, **values
                )
                await s.execute(
                    stmt.on_conflict_do_update(
                        index_elements=[JobPosting.source, JobPosting.external_id], set_=values
                    )
                )
            for o in outcomes:
                if o.existing is None:
                    continue
                if o.kind == "not_modified":
                    self.stats.unchanged += 1
                    await s.execute(
                        update(JobPosting)
                        .where(JobPosting.id == o.existing.id)
                        .values(fetched_at=now, last_seen_at=now, lastmod=o.entry.lastmod)
                    )
                elif o.kind == "gone":
                    self.stats.removed += 1
                    await s.execute(
                        update(JobPosting)
                        .where(JobPosting.id == o.existing.id)
                        .values(status=JobStatus.REMOVED.value, fetched_at=now)
                    )
            if records:
                await s.execute(
                    insert(SourceFetch),
                    [
                        {
                            "run_id": None,
                            "company_id": None,
                            "source": r.source,
                            "url": r.url,
                            "http_status": r.http_status,
                            "proxy_id": r.proxy_id,
                            "duration_ms": r.duration_ms,
                            "items": r.items,
                            "outcome": r.outcome.value,
                            "error": r.error,
                        }
                        for r in records
                    ],
                )

    async def _mark_missing(self, started_at: datetime) -> None:
        """Tin đang mở nhưng không còn trong danh sách đầy đủ: vắng mặt thêm một lượt."""
        async with self.sessionmaker.begin() as s:
            missed = JobPosting.missed_runs + 1
            result = await s.execute(
                update(JobPosting)
                .where(
                    JobPosting.source == self.adapter.name,
                    JobPosting.status != JobStatus.REMOVED.value,
                    JobPosting.last_seen_at < started_at,
                )
                .values(
                    missed_runs=missed,
                    status=case(
                        (missed >= REMOVE_AFTER_MISSED_RUNS, JobStatus.REMOVED.value),
                        else_=JobPosting.status,
                    ),
                )
                .returning(JobPosting.status)
            )
            self.stats.removed += sum(
                1 for (status,) in result if status == JobStatus.REMOVED.value
            )

    async def _mark_indexed(self, started_at: datetime, *, all_profiles: bool) -> None:
        async with self.sessionmaker.begin() as s:
            stmt = update(SourceProfile).where(SourceProfile.source == self.adapter.name)
            if not all_profiles:
                fresh = (
                    select(JobPosting.source_profile_id)
                    .where(
                        JobPosting.source == self.adapter.name,
                        JobPosting.last_seen_at >= started_at,
                        JobPosting.fetched_at.is_not(None),
                    )
                    .distinct()
                )
                stmt = stmt.where(SourceProfile.id.in_(fresh))
            await s.execute(stmt.values(indexed_at=started_at))

    async def _enrich_profile(self, profile: SourceProfile) -> None:
        """Đọc trang profile (website, MST, địa chỉ) để map chắc hơn; có giới hạn mỗi lượt."""
        if self._profile_pages >= self.options.max_profile_pages:
            return
        self._profile_pages += 1
        try:
            result = await self.session.fetch(profile.url, rate=self.adapter.rate)
        except SourceError:
            return
        if not result.ok:
            return
        ref = self.adapter.parse_profile(result.text, profile.url)
        if ref is None:
            return
        profile.website = ref.website or profile.website
        profile.tax_code = ref.tax_code or profile.tax_code
        profile.info = {**(profile.info or {}), **ref.info}

    # --- mạng ---

    async def _fetch(self, entry: BoardEntry, row: _Existing | None) -> _Outcome:
        out = _Outcome(entry=entry, existing=row)
        headers: dict[str, str] = {}
        if row is not None and not self.options.full and row.status != JobStatus.REMOVED.value:
            if row.etag:
                headers["If-None-Match"] = row.etag
            if row.http_last_modified:
                headers["If-Modified-Since"] = row.http_last_modified
        result = await self.session.fetch(
            entry.url, headers=headers or None, rate=self.adapter.rate, use_cache=False
        )
        out.result = result
        if result.status == 304:
            out.kind = "not_modified"
        elif result.status in (404, 410):
            out.kind = "gone"
        elif result.ok:
            try:
                out.parsed = self.adapter.parse_job(result.text, result.final_url or entry.url)
            except Exception:  # một trang lạ không được làm hỏng cả lượt
                log.exception("board.parse_error", url=entry.url, source=self.adapter.source)
                out.parsed = None
            if out.parsed is not None:
                # URL trong DB là URL trong sitemap (ổn định), không phải URL sau redirect.
                out.kind = "parsed"
                self.session.count(entry.url, 1)
        if out.kind == "failed":
            log.info("board.fetch_failed", url=entry.url, status=result.status, error=result.error)
        return out

    async def run(self) -> IndexStats:
        st = self.stats
        started = self.clock()
        started_at = datetime.now(UTC)
        lock_key = f"board-index:{self.adapter.name}"
        async with self.engine.connect() as lock_conn:
            got = await lock_conn.scalar(select(func.pg_try_advisory_lock(func.hashtext(lock_key))))
            if not got:
                st.skipped = "đang được index ở process khác"
                st.status = RunStatus.FAILED
                log.warning("board.index_skipped", source=self.adapter.source, reason=st.skipped)
                return st
            try:
                await self._start_run(started_at)
                await self._run(started, started_at)
            except Exception as exc:
                st.status, st.error = RunStatus.FAILED, f"{type(exc).__name__}: {exc}"
                log.exception("board.index_error", source=self.adapter.source)
            finally:
                st.duration_ms = int((self.clock() - started) * 1000)
                if st.run_id is not None:
                    await asyncio.shield(self._finish_run())
                await lock_conn.execute(select(func.pg_advisory_unlock(func.hashtext(lock_key))))
        log.info(
            "board.index_done",
            source=self.adapter.source,
            status=st.status.value,
            complete=st.complete,
            urls_seen=st.urls_seen,
            fetched=st.fetched,
            new=st.new,
            updated=st.updated,
            unchanged=st.unchanged,
            failed=st.failed,
            removed=st.removed,
            profiles_new=st.profiles_new,
            profiles_matched=st.profiles_matched,
            duration_ms=st.duration_ms,
            error=st.error,
        )
        return st

    def _out_of_time(self, started: float) -> bool:
        budget = self.options.budget_seconds
        return budget is not None and self.clock() - started >= budget

    async def _run(self, started: float, started_at: datetime) -> None:
        st, opts = self.stats, self.options
        listing = ListingState()
        entries: dict[str, BoardEntry] = {}
        try:
            async for entry in self.adapter.entries(self.session, listing):
                entries.setdefault(entry.external_id, entry)
                if self._out_of_time(started):
                    listing.fail("hết thời gian khi đọc danh sách")
                    break
        except SourceError as exc:
            listing.fail(str(exc))
            st.error = str(exc)
        st.urls_seen = len(entries)
        st.listing_errors = listing.errors

        existing = await self._load_existing()
        await self._touch([existing[k].id for k in entries if k in existing], started_at)
        plan = plan_fetches(
            entries, existing, now=started_at, refresh_days=opts.refresh_days, full=opts.full
        )
        st.to_fetch = len(plan)
        if opts.max_jobs is not None and len(plan) > opts.max_jobs:
            plan, st.truncated = plan[: opts.max_jobs], True

        pending: list[_Outcome] = []
        blocked = False
        for i in range(0, len(plan), opts.concurrency):
            if self._out_of_time(started):
                st.truncated = True
                break
            batch = plan[i : i + opts.concurrency]
            try:
                outcomes = await asyncio.gather(*(self._fetch(e, row) for e, row in batch))
            except SourceError as exc:  # bị chặn: dừng nguồn trong lượt này
                st.error, blocked, st.truncated = str(exc), True, True
                break
            st.fetched += len(outcomes)
            st.failed += sum(1 for o in outcomes if o.kind == "failed")
            pending.extend(outcomes)
            if len(pending) >= opts.flush_every:
                await self._flush(pending, datetime.now(UTC))
                pending = []
        await self._flush(pending, datetime.now(UTC))

        st.complete = listing.complete and not blocked
        active = sum(1 for row in existing.values() if row.status == JobStatus.ACTIVE.value)
        if st.complete and active >= MIN_ACTIVE_FOR_DROP_CHECK and len(entries) < active * MAX_DROP:
            # Danh sách tụt bất thường (trang trả trang chắn/lỗi mà không bị nhận ra): không
            # coi phần thiếu là tin đã gỡ.
            st.complete = False
            st.listing_errors.append(f"danh sách tụt bất thường: {len(entries)}/{active} tin")
            log.warning(
                "board.listing_drop", source=self.adapter.source, seen=len(entries), active=active
            )
        if st.complete:
            await self._mark_missing(started_at)
        await self._mark_indexed(started_at, all_profiles=st.complete and not st.truncated)
        if opts.map_profiles and not self._out_of_time(started):
            async with self.sessionmaker.begin() as s:
                st.profiles_matched = await map_profiles(
                    s,
                    source=self.adapter.name,
                    llm=self.llm,
                    trigram_threshold=opts.profile_trigram,
                    min_confidence=opts.profile_min_confidence,
                    enrich=self._enrich_profile,
                )
        if blocked or st.error:
            st.status = RunStatus.PARTIAL if entries else RunStatus.FAILED
        elif not st.complete or st.truncated:
            st.status = RunStatus.PARTIAL
        else:
            st.status = RunStatus.OK
