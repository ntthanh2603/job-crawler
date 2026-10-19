"""Bộ khung thu thập (`crawl_plans`): lưu sau lượt build, nạp cho lượt poll."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from jobcrawler.db.models import CrawlPlan
from jobcrawler.logging import get_logger
from jobcrawler.pipeline.collect import SourceReport
from jobcrawler.schemas import FetchOutcome, PlanStatus
from jobcrawler.sources import ats
from jobcrawler.sources.ats.ref import AtsRef
from jobcrawler.sources.base import Source
from jobcrawler.sources.careers import KIND as CAREERS
from jobcrawler.sources.careers import CareersSource
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


def built_plans(
    reports: Sequence[SourceReport], ats_refs: Sequence[AtsRef] = ()
) -> list[BuiltPlan]:
    """Bộ khung của các nguồn website / ATS trong một lượt build (nguồn trang tuyển dụng không
    có bộ khung: chúng đọc từ index chung)."""
    refs = {ref.name: ref for ref in ats_refs}
    out: list[BuiltPlan] = []
    for report in reports:
        kind = kind_of(report.name)
        if report.plan is None or (kind != CAREERS and report.name not in refs):
            continue
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
    return out


async def save_built_plans(
    session: AsyncSession,
    company_id: UUID,
    plans: Sequence[BuiltPlan],
    *,
    run_id: UUID,
    now: datetime,
    retire_missing: bool,
) -> int:
    """Ghi bộ khung của lượt build; nguồn không còn thấy thì `retired` (chỉ khi lượt build tìm
    nguồn trọn vẹn). Trả số nguồn đang dùng được."""
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
    return sum(p.ok for p in plans)


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
