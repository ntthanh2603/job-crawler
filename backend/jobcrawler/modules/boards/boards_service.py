"""Index các trang tuyển dụng và map profile (gọi từ CLI `index-boards` và job của runner).

- `index_board`: lượt đủ (ban đêm) — cả danh sách, đánh dấu tin gỡ, `indexed_at` của profile.
- `tail_board`: lượt đuôi (ban ngày, vài phút một lần) — phần mới nhất của danh sách, chỉ tin
  chưa có; sau đó runner poll ngay `companies_with_new_postings`.
- `board_sources(network=False)`: lượt poll doanh nghiệp chỉ đọc index, không gọi trang.

Truy vấn lịch sử index trang tuyển dụng cho giao diện.

Thao tác trang tuyển dụng cho API: danh sách trang, xếp lượt index."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from pydantic import Field
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from jobcrawler.common.config import Settings
from jobcrawler.common.logging import get_logger
from jobcrawler.common.pagination import GetManyQuery, Page, ordering, paginate
from jobcrawler.common.schemas import JobStatus, RawJob
from jobcrawler.common.text import company_match_key, distinctive_key
from jobcrawler.common.urls import host_of, registrable_domain
from jobcrawler.database.models import (
    BoardIndexRun,
    Company,
    CompanyAlias,
    JobPosting,
    SourceProfile,
)
from jobcrawler.pipeline import kyc
from jobcrawler.pipeline.board_index import BoardIndexer, IndexOptions, IndexStats
from jobcrawler.pipeline.profiles import decide, map_profiles
from jobcrawler.services.resources import Resources
from jobcrawler.services.serp import SerpClient
from jobcrawler.sources import boards
from jobcrawler.sources.base import FetchRecord, Source, SourceError, SourceLimits, SourceSession
from jobcrawler.sources.boards.base import BoardAdapter
from jobcrawler.sources.boards.profile_source import BoardProfileSource

log = get_logger(__name__)


class UnknownBoardError(ValueError):
    pass


def selected_boards(settings: Settings, names: list[str] | None = None) -> list[BoardAdapter]:
    """Adapter cần chạy: theo `names`, không có thì theo BOARDS, rỗng thì mọi adapter đang bật."""
    registry = boards.registry()
    wanted = names or settings.boards
    if wanted:
        unknown = [n for n in wanted if n not in registry]
        if unknown:
            raise UnknownBoardError(
                f"không có adapter {', '.join(unknown)} (có: {', '.join(registry)})"
            )
        return [registry[n] for n in wanted]
    return [a for a in registry.values() if a.enabled]


def index_options(
    settings: Settings,
    *,
    max_jobs: int | None = None,
    budget_seconds: float | None = None,
    full: bool = False,
    map_profiles: bool = True,
) -> IndexOptions:
    return IndexOptions(
        max_jobs=max_jobs if max_jobs is not None else settings.board_max_jobs_per_run,
        budget_seconds=budget_seconds or settings.board_index_budget_seconds,
        refresh_days=settings.board_refresh_days,
        concurrency=settings.board_concurrency,
        full=full,
        map_profiles=map_profiles,
        profile_trigram=settings.profile_match_trigram,
        profile_min_confidence=settings.profile_match_min_confidence,
    )


# Lượt đuôi: trang profile đọc thêm (website/MST) khi map profile mới, mỗi lượt.
TAIL_PROFILE_PAGES = 5


def tail_options(
    settings: Settings, *, max_jobs: int | None = None, budget_seconds: float | None = None
) -> IndexOptions:
    """Lượt đuôi: BOARD_TAIL_BUDGET_SECONDS, tối đa BOARD_TAIL_MAX_JOBS trang chi tiết."""
    return IndexOptions(
        tail=True,
        max_jobs=max_jobs if max_jobs is not None else settings.board_tail_max_jobs,
        budget_seconds=budget_seconds or settings.board_tail_budget_seconds,
        concurrency=settings.board_concurrency,
        profile_trigram=settings.profile_match_trigram,
        profile_min_confidence=settings.profile_match_min_confidence,
        max_profile_pages=TAIL_PROFILE_PAGES,
    )


async def index_board(res: Resources, adapter: BoardAdapter, options: IndexOptions) -> IndexStats:
    """Một lượt index (đủ hay đuôi theo `options.tail`) một trang, qua tầng mạng crawl."""
    indexer = BoardIndexer(
        adapter,
        redis=res.redis,  # khoá index một trang: Redis chính
        redis_prefix=res.settings.redis_prefix,
        sessionmaker=res.sessionmaker,
        fetcher=res.http,
        llm=res.llm,
        options=options,
        limits=SourceLimits.from_settings(res.settings),
    )
    return await indexer.run()


async def tail_board(res: Resources, adapter: BoardAdapter) -> IndexStats:
    """Lượt đuôi một trang: phần mới nhất của danh sách, chỉ tải tin chưa có. Trang đang được
    index (lượt đủ ban đêm) thì bỏ qua ngay (`IndexStats.skipped`)."""
    return await index_board(res, adapter, tail_options(res.settings))


async def index_boards(
    res: Resources,
    names: list[str] | None = None,
    *,
    max_jobs: int | None = None,
    budget_seconds: float | None = None,
    full: bool = False,
    tail: bool = False,
) -> list[IndexStats]:
    """Index song song các trang (rate limit theo domain nên không giẫm chân nhau). `tail`:
    lượt đuôi thay vì lượt đủ (`full` không áp dụng)."""
    adapters = selected_boards(res.settings, names)
    if tail:
        options = tail_options(res.settings, max_jobs=max_jobs, budget_seconds=budget_seconds)
    else:
        options = index_options(
            res.settings, max_jobs=max_jobs, budget_seconds=budget_seconds, full=full
        )
    return list(await asyncio.gather(*(index_board(res, a, options) for a in adapters)))


async def companies_with_new_postings(res: Resources, source: str, since: datetime) -> list[UUID]:
    """Doanh nghiệp có profile (đã map) trên `source` vừa có tin mới (`first_seen_at >= since`)
    hoặc vừa được map (`matched_at >= since`) — để runner poll ngay sau một lượt index."""
    fresh_jobs = (
        select(SourceProfile.company_id)
        .join(JobPosting, JobPosting.source_profile_id == SourceProfile.id)
        .where(
            JobPosting.source == source,
            JobPosting.first_seen_at >= since,
            SourceProfile.company_id.is_not(None),
        )
    )
    new_profiles = select(SourceProfile.company_id).where(
        SourceProfile.source == source,
        SourceProfile.matched_at >= since,
        SourceProfile.company_id.is_not(None),
    )
    async with res.sessionmaker() as session:
        rows = await session.scalars(
            select(Company.id)
            .where(or_(Company.id.in_(fresh_jobs), Company.id.in_(new_profiles)))
            .order_by(Company.id)
        )
        return list(rows)


STRONG_SIGNALS = frozenset({"mst", "website"})
# Profile nào của doanh nghiệp được dùng, theo mức chắc chắn của KYC:
# - "all": mọi profile đã map (KYC đã xác minh);
# - "strong": chỉ profile khớp MST/website (KYC mơ hồ);
# - "exact_name": thêm profile khớp tên bằng rule mà khoá tên trùng hẳn tên doanh nghiệp và đủ
#   riêng (doanh nghiệp không tìm được website, chỉ có tên).
ProfilePolicy = Literal["all", "strong", "exact_name"]


@dataclass(frozen=True, slots=True)
class _ProfileIndex:
    """Tin của một profile trong index (`job_postings`), cho `BoardProfileSource`."""

    res: Resources
    source: str
    profile_id: UUID

    async def for_profile(self) -> list[RawJob]:
        async with self.res.sessionmaker() as session:
            raws = await session.scalars(
                select(JobPosting.raw).where(
                    JobPosting.source_profile_id == self.profile_id,
                    JobPosting.status == JobStatus.ACTIVE.value,
                )
            )
            return [RawJob.model_validate(raw) for raw in raws]

    async def by_ids(self, external_ids: Sequence[str]) -> dict[str, RawJob]:
        if not external_ids:
            return {}
        async with self.res.sessionmaker() as session:
            rows = await session.execute(
                select(JobPosting.external_id, JobPosting.raw).where(
                    JobPosting.source == self.source,
                    JobPosting.external_id.in_(list(external_ids)),
                    JobPosting.status != JobStatus.REMOVED.value,
                    or_(
                        JobPosting.source_profile_id.is_(None),
                        JobPosting.source_profile_id == self.profile_id,
                    ),
                )
            )
            return {external_id: RawJob.model_validate(raw) for external_id, raw in rows}


def _usable(profile: SourceProfile, policy: ProfilePolicy, names: Sequence[str]) -> bool:
    evidence = profile.match_evidence or {}
    signal = evidence.get("signal")
    if policy == "all" or signal in STRONG_SIGNALS:
        return True
    if policy == "strong" or signal != "name" or not evidence.get("rule"):
        return False
    key = company_match_key(profile.name_on_source)
    return distinctive_key(key) and key in {company_match_key(n) for n in names}


async def board_sources(
    res: Resources,
    company_id: UUID,
    *,
    policy: ProfilePolicy = "all",
    names: Sequence[str] = (),
    now: datetime | None = None,
    network: bool = True,
) -> list[Source]:
    """Nguồn tin của doanh nghiệp trên các trang tuyển dụng (profile đã map, lọc theo
    `policy`; `names`: tên đầu vào/pháp lý/thương hiệu, cho "exact_name").

    `network=False` (lượt poll): chỉ tin trong index, không bao giờ gọi trang tuyển dụng kể cả
    khi index của profile đã cũ hơn BOARD_STALE_HOURS."""
    now = now or datetime.now(UTC)
    stale_before = now - timedelta(hours=res.settings.board_stale_hours)
    registry = boards.registry()
    async with res.sessionmaker() as session:
        profiles = (
            await session.scalars(
                select(SourceProfile)
                .where(SourceProfile.company_id == company_id)
                .order_by(SourceProfile.id)
            )
        ).all()
        # Profile đang có tin trong index trước: doanh nghiệp lớn có hàng chục profile (mỗi chi
        # nhánh, bản trùng trên JobsGO), phần lớn không có tin.
        active = dict(
            (
                await session.execute(
                    select(JobPosting.source_profile_id, func.count())
                    .where(
                        JobPosting.source_profile_id.in_([p.id for p in profiles]),
                        JobPosting.status == JobStatus.ACTIVE.value,
                    )
                    .group_by(JobPosting.source_profile_id)
                )
            ).all()
        )
    profiles = sorted(profiles, key=lambda p: -active.get(p.id, 0))
    reads: Counter[str] = Counter()  # profile được đọc trang tuyển dụng, mỗi trang
    sources: list[Source] = []
    for profile in profiles:
        adapter = registry.get(profile.source)
        if adapter is None or not adapter.enabled:
            continue
        if not _usable(profile, policy, names):
            continue
        fresh = profile.indexed_at is not None and profile.indexed_at >= stale_before
        online = network and not fresh and reads[profile.source] < MAX_PROFILE_READS
        if online:
            reads[profile.source] += 1
        sources.append(
            BoardProfileSource(
                adapter,
                external_id=profile.external_id,
                url=profile.url,
                fresh=fresh,
                index=_ProfileIndex(res, profile.source, profile.id),
                name=profile.name_on_source,
                network=online,
            )
        )
    return sources


# Mỗi lượt build đọc trang tuyển dụng cho tối đa chừng này profile chưa đủ tin trong index của
# một doanh nghiệp trên một trang; profile còn lại chỉ dùng tin đã index.
MAX_PROFILE_READS = 5


MAX_SERP_PROFILES = 3
# Profile chưa map xét cho một doanh nghiệp trong lượt build (tên gần alias).
MAX_COMPANY_PROFILES = 50


async def map_company_profiles(res: Resources, company_id: UUID) -> int:
    """Profile đã có trong bảng nhưng chưa map (index gặp tin trước khi doanh nghiệp có alias
    khớp, profile thêm từ danh bạ nhà tuyển dụng) có tên gần alias của doanh nghiệp: map ngay bằng
    rule trong lượt build, không chờ lượt index đủ ban đêm. Profile còn mơ hồ để lượt index xét
    (đọc trang profile, LLM). Trả số profile map được."""
    s = res.settings
    async with res.sessionmaker.begin() as session:
        await session.execute(
            text("SELECT set_config('pg_trgm.similarity_threshold', :t, true)"),
            {"t": str(s.profile_match_trigram)},
        )
        ids = list(
            await session.scalars(
                select(SourceProfile.id)
                .join(CompanyAlias, CompanyAlias.alias_norm.op("%")(SourceProfile.name_norm))
                .where(CompanyAlias.company_id == company_id, SourceProfile.company_id.is_(None))
                .distinct()
                .limit(MAX_COMPANY_PROFILES)
            )
        )
        matched = await map_profiles(
            session,
            profile_ids=ids,
            trigram_threshold=s.profile_match_trigram,
            min_confidence=s.profile_match_min_confidence,
            retry=True,
            record_misses=False,
        )
    if matched:
        log.info("boards.company_profiles", company_id=company_id, matched=matched)
    return matched


async def lookup_profiles(
    res: Resources, company: Company, *, timeout: float, records: list[FetchRecord]
) -> int:
    """Profile của doanh nghiệp trên các trang tuyển dụng, trước khi đọc tin. Trả số profile mới.

    1. Profile đã có trong bảng, chưa map, tên gần alias (`map_company_profiles`).
    2. Trang chưa có profile nào của doanh nghiệp: tìm `"<tên>" site:...` bằng SERP, mở trang
       profile tìm được và map bằng rule (tên/website/MST khớp). Không có SERP_API_KEY (hoặc
       BOARD_SERP_LOOKUP=false) thì bỏ qua. SERP gọi thẳng từ backend (`api_http`); trang profile
       tìm được thì đi qua tầng mạng crawl (`http`)."""
    s = res.settings
    found = await map_company_profiles(res, company.id)
    serp = SerpClient.from_settings(s, res.api_http, res.cache_kv)
    if not s.board_serp_lookup or not serp.enabled:
        return found
    async with res.sessionmaker() as session:
        mapped = set(
            await session.scalars(
                select(SourceProfile.source).where(SourceProfile.company_id == company.id)
            )
        )
    remaining = [a for a in selected_boards(s) if a.name not in mapped]
    name = company.legal_name or company.brand_name or company.input_name
    if not remaining or not name:
        return found
    sites = " OR ".join(f"site:{registrable_domain(host_of(a.home))}" for a in remaining)
    try:
        async with asyncio.timeout(timeout):
            response = await serp.search(f'"{name}" ({sites})', records=records)
            candidates: list[tuple[BoardAdapter, str]] = []
            for item in response.items:
                for adapter in remaining:
                    if adapter.profile_id(item.url) and all(u != item.url for _, u in candidates):
                        candidates.append((adapter, item.url))
            for adapter, url in candidates[:MAX_SERP_PROFILES]:
                if await _adopt_profile(res, company, adapter, url, records):
                    found += 1
    except TimeoutError:
        log.info("boards.serp_lookup_timeout", company_id=company.id)
    return found


async def _adopt_profile(
    res: Resources, company: Company, adapter: BoardAdapter, url: str, records: list[FetchRecord]
) -> bool:
    session = SourceSession(adapter.source, res.http, res.llm, SourceLimits(), records)
    try:
        page = await session.fetch(url, rate=adapter.rate)
    except SourceError:
        return False
    ref = adapter.parse_profile(page.text, page.final_url) if page.ok else None
    if ref is None:
        if page.ok:
            session.fail_page(page, "no_profile: không bóc được trang profile")
        return False
    candidate = SourceProfile(
        source=adapter.name,
        external_id=ref.external_id,
        url=ref.url,
        name_on_source=ref.name,
        name_norm=company_match_key(ref.name),
        website=ref.website,
        tax_code=ref.tax_code,
        info=ref.info,
    )
    names = [n for n in (company.legal_name, company.brand_name, company.input_name) if n]
    decision = decide(candidate, [(company, company_match_key(n), 1.0) for n in names])
    if decision is None or decision.company_id is None:
        log.info("boards.serp_profile_rejected", url=url, profile=ref.name)
        return False
    async with res.sessionmaker.begin() as db:
        stmt = pg_insert(SourceProfile).values(
            source=adapter.name,
            external_id=ref.external_id,
            url=ref.url,
            name_on_source=ref.name,
            name_norm=candidate.name_norm,
            website=ref.website,
            tax_code=ref.tax_code,
            info=ref.info,
            company_id=company.id,
            match_confidence=decision.confidence,
            match_evidence={**decision.evidence, "via": "serp"},
            matched_at=datetime.now(UTC),
        )
        adopted = await db.scalar(
            stmt.on_conflict_do_update(
                index_elements=[SourceProfile.source, SourceProfile.external_id],
                set_={
                    "company_id": stmt.excluded.company_id,
                    "match_confidence": stmt.excluded.match_confidence,
                    "match_evidence": stmt.excluded.match_evidence,
                    "matched_at": stmt.excluded.matched_at,
                    "website": func.coalesce(stmt.excluded.website, SourceProfile.website),
                },
                where=SourceProfile.company_id.is_(None),
            ).returning(SourceProfile.id)
        )
    if adopted is None:
        # Profile đã map vào doanh nghiệp khác: không ghi đè, không tính là tìm được.
        log.info("boards.serp_profile_taken", source=adapter.name, url=url, company_id=company.id)
        return False
    log.info("boards.serp_profile", source=adapter.name, url=url, company_id=company.id)
    return True


@dataclass(slots=True)
class MapReport:
    matched: int
    profiles: int
    mapped_total: int


@dataclass(slots=True)
class RematchReport:
    renamed: int  # profile có khoá tên mới
    aliases: int  # alias khoá map mới của doanh nghiệp
    mapped: MapReport


_REMATCH_CHUNK = 1000


async def rematch_profiles(res: Resources, names: list[str] | None = None) -> RematchReport:
    """Sau khi đổi cách so tên (`company_match_key`): tính lại khoá tên của profile, thêm alias
    khoá map cho mọi doanh nghiệp, cho profile chưa map được thử lại, rồi map lại."""
    sources = [a.name for a in selected_boards(res.settings, names)]
    async with res.sessionmaker.begin() as session:
        rows = (
            await session.execute(
                select(
                    SourceProfile.id, SourceProfile.name_on_source, SourceProfile.name_norm
                ).where(SourceProfile.source.in_(sources))
            )
        ).all()
        renamed = [
            {"id": pid, "name_norm": key}
            for pid, name, norm in rows
            if (key := company_match_key(name)) != norm
        ]
        for i in range(0, len(renamed), _REMATCH_CHUNK):
            await session.execute(update(SourceProfile), renamed[i : i + _REMATCH_CHUNK])
        await session.execute(
            update(SourceProfile)
            .where(SourceProfile.source.in_(sources), SourceProfile.company_id.is_(None))
            .values(matched_at=None)
        )
        aliases = (
            await session.execute(
                select(CompanyAlias.company_id, CompanyAlias.alias, CompanyAlias.kind).where(
                    CompanyAlias.kind.is_distinct_from(kyc.MATCH_ALIAS)
                )
            )
        ).all()
        match_rows = [
            row
            for company_id, alias, kind in aliases
            for row in kyc.alias_rows(company_id, alias, kind or "other")
            if row["kind"] == kyc.MATCH_ALIAS
        ]
        added = 0
        for i in range(0, len(match_rows), _REMATCH_CHUNK):
            result = await session.execute(
                pg_insert(CompanyAlias)
                .values(match_rows[i : i + _REMATCH_CHUNK])
                .on_conflict_do_nothing(index_elements=["company_id", "alias_norm"])
                .returning(CompanyAlias.id)
            )
            added += len(result.all())
    log.info("boards.rematch", renamed=len(renamed), aliases=added)
    return RematchReport(
        renamed=len(renamed), aliases=added, mapped=await map_all_profiles(res, names)
    )


async def map_all_profiles(res: Resources, names: list[str] | None = None) -> MapReport:
    """Map lại profile chưa map của các trang (vd. sau khi nạp doanh nghiệp mới)."""
    s = res.settings
    matched = 0
    async with res.sessionmaker.begin() as session:
        for adapter in selected_boards(s, names):
            matched += await map_profiles(
                session,
                source=adapter.name,
                llm=res.llm,
                trigram_threshold=s.profile_match_trigram,
                min_confidence=s.profile_match_min_confidence,
            )
        profiles = await session.scalar(select(func.count()).select_from(SourceProfile)) or 0
        mapped = (
            await session.scalar(
                select(func.count())
                .select_from(SourceProfile)
                .where(SourceProfile.company_id.is_not(None))
            )
            or 0
        )
    return MapReport(matched=matched, profiles=profiles, mapped_total=mapped)


# --- truy vấn cho giao diện -----------------------------------------------------------------------

BOARD_RUN_SORTS: dict[str, tuple[Any, bool]] = {
    "started_at": (BoardIndexRun.started_at, False),
    "new": (BoardIndexRun.new, False),
    "fetched": (BoardIndexRun.fetched, False),
    "failed": (BoardIndexRun.failed, False),
}


class BoardRunsQuery(GetManyQuery):
    """`GET /api/boards/runs`: `search` trong tên trang tuyển dụng."""

    mode: str | None = Field(default=None, description="full | tail")
    sortBy: Literal["started_at", "new", "fetched", "failed"] = "started_at"


async def board_runs(session: AsyncSession, f: BoardRunsQuery | None = None) -> Page[BoardIndexRun]:
    """Lịch sử index trang tuyển dụng; `mode`: `full` (đủ, đêm) | `tail` (đuôi, ngày)."""
    f = f or BoardRunsQuery()
    stmt = select(BoardIndexRun)
    if f.mode == "tail":
        stmt = stmt.where(BoardIndexRun.stats["mode"].astext == "tail")
    elif f.mode == "full":
        stmt = stmt.where(func.coalesce(BoardIndexRun.stats["mode"].astext, "full") == "full")
    if search := f.search.strip():
        stmt = stmt.where(BoardIndexRun.source.ilike(f"%{search}%"))
    key, nullable = BOARD_RUN_SORTS[f.sortBy]
    return await paginate(
        session,
        stmt,
        f.page,
        limit=f.limit,
        order_by=ordering(key, f.sortOrder, BoardIndexRun.id, nullable=nullable),
    )


# --- thao tác cho API -----------------------------------------------------------------------------


def board_job_timeout(settings: Settings) -> int:
    """Hạn của job index đủ: ngân sách của lượt index + thời gian ghi DB/map profile cuối lượt."""
    return int(settings.board_index_budget_seconds) + 900


def board_tail_timeout(settings: Settings) -> int:
    """Hạn của job index đuôi."""
    return int(settings.board_tail_budget_seconds) + 120


def board_names() -> list[str]:
    return list(boards.registry())


async def enqueue_board(res: Resources, name: str) -> bool:
    """Đưa một trang tuyển dụng vào queue index (runner vai `boards`). False nếu trang đó đang
    chờ/đang index."""
    if name not in boards.registry():
        raise KeyError(name)
    job = await res.queues.boards.enqueue(
        "index_board",
        source=name,
        key=f"board:{name}",
        timeout=board_job_timeout(res.settings),
    )
    return job is not None
