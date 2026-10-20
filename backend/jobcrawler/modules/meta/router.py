"""API `/api/meta`: giá trị cho các bộ lọc của giao diện."""

from __future__ import annotations

from fastapi import APIRouter, Request

from jobcrawler.common.api import res_of
from jobcrawler.common.schemas import EmploymentType, JobLevel, KycStatus, WorkMode
from jobcrawler.modules.boards import actions as board_actions
from jobcrawler.modules.jobs import queries as job_queries
from jobcrawler.modules.meta.schemas import MetaOut
from jobcrawler.modules.runs import queries as run_queries

router = APIRouter(prefix="/api", tags=["api"])


@router.get("/meta")
async def get_meta(request: Request) -> MetaOut:
    """Giá trị cho các bộ lọc."""
    async with res_of(request).sessionmaker() as session:
        sources = await job_queries.job_sources(session)
    return MetaOut(
        provinces=job_queries.PROVINCE_NAMES,
        levels=job_queries.enum_values(JobLevel),
        employment_types=job_queries.enum_values(EmploymentType),
        work_modes=job_queries.enum_values(WorkMode),
        sources=sources,
        kyc_statuses=[s.value for s in KycStatus],
        run_statuses=run_queries.RUN_STATUSES,
        boards=board_actions.board_names(),
    )
