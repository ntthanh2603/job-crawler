"""Truy vấn bộ khung và lịch thu thập của một doanh nghiệp cho giao diện."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jobcrawler.database.models import CompanySchedule, CrawlPlan


async def company_crawl(
    session: AsyncSession, company_id: UUID
) -> tuple[CompanySchedule | None, list[CrawlPlan]]:
    """Lịch thu thập và bộ khung các nguồn (đang dùng trước, nguồn đã bỏ sau cùng)."""
    schedule = await session.get(CompanySchedule, company_id)
    plans = await session.scalars(
        select(CrawlPlan)
        .where(CrawlPlan.company_id == company_id)
        .order_by((CrawlPlan.status == "retired").asc(), CrawlPlan.source)
    )
    return schedule, list(plans)
