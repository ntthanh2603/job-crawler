"""API `/api/meta`: giá trị cho các bộ lọc của giao diện."""

from __future__ import annotations

from fastapi import APIRouter, Request

from jobcrawler.common.api import res_of
from jobcrawler.common.schemas import EmploymentType, JobLevel, KycStatus, WorkMode
from jobcrawler.modules.boards import boards_service
from jobcrawler.modules.jobs import jobs_service
from jobcrawler.modules.meta.meta_schema import MetaOut
from jobcrawler.modules.runs import runs_service

router = APIRouter(prefix="/api", tags=["meta"])


@router.get("/meta")
async def get_meta(request: Request) -> MetaOut:
    """Giá trị cho các bộ lọc."""
    async with res_of(request).sessionmaker() as session:
        sources = await jobs_service.job_sources(session)
    return MetaOut(
        provinces=jobs_service.PROVINCE_NAMES,
        levels=jobs_service.enum_values(JobLevel),
        employment_types=jobs_service.enum_values(EmploymentType),
        work_modes=jobs_service.enum_values(WorkMode),
        sources=sources,
        kyc_statuses=[s.value for s in KycStatus],
        run_statuses=runs_service.RUN_STATUSES,
        boards=boards_service.board_names(),
    )
