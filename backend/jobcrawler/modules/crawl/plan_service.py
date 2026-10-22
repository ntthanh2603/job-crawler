"""Bộ khung thu thập (`crawl_plans`): lưu sau lượt build, nạp cho lượt poll."""

from __future__ import annotations

import hashlib
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from jobcrawler.common.logging import get_logger
from jobcrawler.common.schemas import FetchOutcome, PlanStatus
from jobcrawler.database.models import CrawlPlan
from jobcrawler.pipeline import store
from jobcrawler.pipeline.collect import SourceReport
from jobcrawler.sources import ats
from jobcrawler.sources.ats.ref import AtsRef
from jobcrawler.sources.base import Source
from jobcrawler.sources.boards import registry as board_registry
from jobcrawler.sources.careers import JOB_BOARD_PAGE, CareersSource, job_board_host
from jobcrawler.sources.careers import KIND as CAREERS
from jobcrawler.sources.plan import PlanSpec, load_spec

log = get_logger(__name__)


def signature(keys: Iterable[str]) -> str:
    return hashlib.sha1("\n".join(sorted(set(keys))).encode()).hexdigest()


def kind_of(source_name: str) -> str:
    return source_name.split(":", 1)[0]


@dataclass(slots=True)
class BuiltPlan:
    """Bộ khung một nguồn ghi được ở lượt build."""

    source: str
    kind: str
    url: str
    spec: PlanSpec
    keys: list[str]
    ok: bool  # nguồn chạy được (có thể bị cắt vì giới hạn)
    error: str | None = None


def allocate_pages(
    demands: Sequence[int], *, max_pages: int, min_pages: int, growable: Sequence[bool] = ()
) -> list[int]:
    """Chia `max_pages` trang của một doanh nghiệp cho các nguồn (theo thứ tự): trước hết mỗi
    nguồn tới `min(nhu cầu, min_pages)`, rồi chia vòng từng trang tới đủ nhu cầu; trang còn dư chia
    vòng cho nguồn `growable` (trang careers: site thêm trang sau thì lượt poll vẫn đọc trọn)."""
    alloc = [0] * len(demands)
    left = max_pages
    for i, need in enumerate(demands):
        give = min(need, min_pages, left)
        alloc[i], left = give, left - give
    while left > 0 and any(a < d for a, d in zip(alloc, demands, strict=True)):
        for i, need in enumerate(demands):
            if left > 0 and alloc[i] < need:
                alloc[i], left = alloc[i] + 1, left - 1
    grow = [i for i, g in enumerate(growable) if g and demands[i] > 0]
    while left > 0 and grow:
        for i in grow:
            if left > 0:
                alloc[i], left = alloc[i] + 1, left - 1
    return alloc


def job_board_source(report: SourceReport) -> bool:
    """Nguồn careers hoá ra là trang tin nhiều nhà tuyển dụng (`careers.JOB_BOARD_PAGE`)."""
    return (report.error or "").startswith(JOB_BOARD_PAGE)


def built_plans(
    reports: Sequence[SourceReport],
    ats_refs: Sequence[AtsRef] = (),
    *,
    max_pages: int = 10,
    min_pages: int = 3,
) -> list[BuiltPlan]:
    """Bộ khung của các nguồn website / ATS trong một lượt build (nguồn trang tuyển dụng không
    có bộ khung: chúng đọc từ index chung). Mỗi bộ khung được phần trang poll của mình
    (`PlanSpec.pages`, `allocate_pages`)."""
    refs = {ref.name: ref for ref in ats_refs}
    out: list[BuiltPlan] = []
    for report in reports:
        kind = kind_of(report.name)
        if report.plan is None or (kind != CAREERS and report.name not in refs):
            continue
        if job_board_source(report):
            continue  # không phải nguồn của doanh nghiệp: không ghi bộ khung
        spec = report.plan
        if report.name in refs:
            spec.ats = refs[report.name].model_dump(mode="json")
        ok = report.outcome is FetchOutcome.OK or report.jobs > 0
        if kind == CAREERS and not spec.listings:
            ok = False  # không đọc được trang danh sách nào: không có gì để poll
        out.append(
            BuiltPlan(
                source=report.name,
                kind=kind,
                url=report.url,
                spec=spec,
                keys=list(dict.fromkeys(report.keys)),
                ok=ok,
                error=report.error,
            )
        )
    shares = allocate_pages(
        [p.spec.demand() if p.ok else 0 for p in out],
        max_pages=max_pages,
        min_pages=min_pages,
        growable=[p.ok and p.kind == CAREERS for p in out],
    )
    for plan, pages in zip(out, shares, strict=True):
        plan.spec.pages = pages
    return out


async def save_built_plans(
    session: AsyncSession,
    company_id: UUID,
    plans: Sequence[BuiltPlan],
    *,
    run_id: UUID,
    now: datetime,
    retire_missing: bool,
    job_boards: Collection[str] = (),
) -> int:
    """Ghi bộ khung của lượt build; nguồn không còn thấy thì `retired` (chỉ khi lượt build tìm
    nguồn trọn vẹn). `job_boards`: nguồn careers lượt này thấy là trang của sàn (bỏ luôn). Trả số
    nguồn đang dùng được."""
    for plan in plans:
        values = {
            "kind": plan.kind,
            "url": plan.url,
            "spec": plan.spec.model_dump(mode="json"),
            "status": (PlanStatus.ACTIVE if plan.ok else PlanStatus.BROKEN).value,
            "jobs": len(plan.keys),
            "job_keys": plan.keys,
            "signature": signature(plan.keys),
            "built_run_id": run_id,
            "built_at": now,
            "failures": 0,
            "last_error": None if plan.ok else (plan.error or "build lỗi")[:500],
        }
        stmt = pg_insert(CrawlPlan).values(company_id=company_id, source=plan.source, **values)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[CrawlPlan.company_id, CrawlPlan.source], set_=values
            )
        )
    if retire_missing:
        await session.execute(
            update(CrawlPlan)
            .where(
                CrawlPlan.company_id == company_id,
                CrawlPlan.source.not_in([p.source for p in plans] or [""]),
                CrawlPlan.status != PlanStatus.RETIRED.value,
            )
            .values(status=PlanStatus.RETIRED.value, last_error="lượt build không còn thấy nguồn")
        )
    await _retire_job_board_plans(session, company_id, plans, job_boards, now=now)
    return sum(p.ok for p in plans)


async def _retire_job_board_plans(
    session: AsyncSession,
    company_id: UUID,
    plans: Sequence[BuiltPlan],
    job_boards: Collection[str],
    *,
    now: datetime,
) -> None:
    """Nguồn careers trên chính sàn tuyển dụng (ghi từ trước khi có luật `job_board_host`) hay
    lượt build vừa thấy là trang tin nhiều nhà tuyển dụng (`job_boards`): bỏ dù lượt tìm nguồn
    chưa trọn (site của công ty sở hữu sàn rất lớn, hay hết giờ), và gỡ các tin nó đã ghi — đó là
    tin của doanh nghiệp khác đăng trên sàn."""
    rows = await session.execute(
        select(
            CrawlPlan.id, CrawlPlan.source, CrawlPlan.kind, CrawlPlan.url, CrawlPlan.job_keys
        ).where(
            CrawlPlan.company_id == company_id,
            CrawlPlan.kind == CAREERS,
            CrawlPlan.source.not_in([p.source for p in plans] or [""]),
            CrawlPlan.status != PlanStatus.RETIRED.value,
        )
    )
    stale = [row for row in rows if row.source in job_boards or job_board_host(row.url)]
    if not stale:
        return
    await session.execute(
        update(CrawlPlan)
        .where(CrawlPlan.id.in_([row.id for row in stale]))
        .values(status=PlanStatus.RETIRED.value, last_error="trang của sàn tuyển dụng")
    )
    present = {(p.kind, key) for p in plans for key in p.keys}
    closed = await store.close_postings(
        session,
        company_id,
        {(row.kind, key) for row in stale for key in row.job_keys or []} - present,
        present=present,
        boards=set(board_registry()),
        now=now,
    )
    log.info("plans.job_board_retired", plans=len(stale), closed=closed)


async def active_plans(session: AsyncSession, company_id: UUID) -> list[CrawlPlan]:
    rows = await session.scalars(
        select(CrawlPlan)
        .where(CrawlPlan.company_id == company_id, CrawlPlan.status == PlanStatus.ACTIVE.value)
        .order_by(CrawlPlan.source)
    )
    return list(rows)


async def has_broken_plans(session: AsyncSession, company_id: UUID) -> bool:
    found = await session.scalar(
        select(CrawlPlan.id)
        .where(CrawlPlan.company_id == company_id, CrawlPlan.status == PlanStatus.BROKEN.value)
        .limit(1)
    )
    return found is not None


def plan_source(plan: CrawlPlan) -> tuple[Source, PlanSpec] | None:
    """Dựng lại nguồn từ bộ khung đã lưu (None: bộ khung cũ / không dựng được → build lại)."""
    spec = load_spec(plan.spec)
    if spec is None:
        return None
    if plan.kind == CAREERS:
        if not spec.listings and not spec.apis:
            return None
        return CareersSource(plan.url), spec
    if spec.ats is None:
        return None
    source = ats.source_for(AtsRef.model_validate(spec.ats))
    return (source, spec) if source is not None else None
