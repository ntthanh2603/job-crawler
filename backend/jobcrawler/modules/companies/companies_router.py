"""API doanh nghiệp: `/api/companies…` (danh sách, chi tiết, thêm, import CSV) và `/api/enqueue`."""

from __future__ import annotations

import dataclasses
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, Response, UploadFile

from jobcrawler.common.api import PageOut, actor_of, page_out, res_of
from jobcrawler.common.schemas import CompanyEntity
from jobcrawler.modules.audit import audit_service
from jobcrawler.modules.companies import batch_service, companies_service
from jobcrawler.modules.companies.companies_schema import (
    AliasOut,
    CompanyCreatedOut,
    CompanyCreateIn,
    CompanyDetailOut,
    CompanyItem,
    EnqueueIn,
    EnqueueResultOut,
    ImportOutcomeOut,
    ProfileOut,
)
from jobcrawler.modules.jobs import jobs_service
from jobcrawler.modules.jobs.jobs_schema import PostingOut, posting_out
from jobcrawler.modules.runs.runs_schema import RunOut, failed_page_item, run_out

router = APIRouter(prefix="/api", tags=["companies"])


@router.get("/companies")
async def get_companies(
    request: Request, query: Annotated[companies_service.CompanyListQuery, Query()]
) -> PageOut[CompanyItem]:
    """Doanh nghiệp theo bộ lọc, một trang."""
    async with res_of(request).sessionmaker() as session:
        result = await companies_service.list_companies(session, query)
    return page_out(
        result,
        [
            CompanyItem(
                company=CompanyEntity.model_validate(c),
                input_website=c.input_website,
                last_status=c.last_run_status,
                last_started=c.last_run_at,
                last_duration_ms=c.last_run_duration_ms,
                last_incomplete=list(c.last_run_incomplete or []),
                active_jobs=c.active_jobs,
            )
            for c in result.items
        ],
    )


@router.get("/companies/{company_id}")
async def get_company(request: Request, company_id: UUID) -> CompanyDetailOut:
    async with res_of(request).sessionmaker() as session:
        d = await companies_service.company_detail(session, company_id)
    if d is None:
        raise HTTPException(404, "Không có doanh nghiệp này")
    c = d.company
    return CompanyDetailOut(
        company=CompanyEntity.model_validate(c),
        input_website=c.input_website,
        note=c.note,
        created_at=c.created_at,
        kyc_evidence=c.kyc_evidence,
        aliases=[AliasOut(alias=a.alias, kind=a.kind) for a in d.aliases],
        profiles=[
            ProfileOut(
                source=p.source,
                url=p.url,
                name_on_source=p.name_on_source,
                website=p.website,
                tax_code=p.tax_code,
                match_confidence=p.match_confidence,
                match_signal=p.match_evidence.get("signal")
                if isinstance(p.match_evidence, dict)
                else None,
                indexed_at=p.indexed_at,
            )
            for p in d.profiles
        ],
        active_jobs=c.active_jobs,
        inactive_jobs=d.inactive_jobs,
        runs_total=d.runs,
        rejected_total=d.rejected,
        failed_pages=[failed_page_item(f) for f in d.failed_pages],
    )


@router.get("/companies/{company_id}/jobs")
async def get_company_jobs(
    request: Request,
    company_id: UUID,
    query: Annotated[companies_service.CompanyJobsQuery, Query()],
) -> PageOut[jobs_service.JobItem]:
    async with res_of(request).sessionmaker() as session:
        result = await companies_service.company_jobs(session, company_id, query)
    return page_out(result, result.items)


@router.get("/companies/{company_id}/runs")
async def get_company_runs(
    request: Request,
    company_id: UUID,
    query: Annotated[companies_service.CompanyRunsQuery, Query()],
) -> PageOut[RunOut]:
    async with res_of(request).sessionmaker() as session:
        result = await companies_service.company_runs(session, company_id, query)
    return page_out(result, [run_out(r) for r in result.items])


@router.get("/companies/{company_id}/rejected")
async def get_company_rejected(
    request: Request,
    company_id: UUID,
    query: Annotated[companies_service.CompanyRejectedQuery, Query()],
) -> PageOut[PostingOut]:
    async with res_of(request).sessionmaker() as session:
        result = await companies_service.company_rejected(session, company_id, query)
    return page_out(result, [posting_out(p) for p in result.items])


@router.post("/companies")
async def post_company(request: Request, body: CompanyCreateIn) -> CompanyCreatedOut:
    """Thêm một doanh nghiệp bằng tay. Đã có (cùng MST, hoặc cùng tên không mâu thuẫn MST /
    website) thì không tạo trùng, chỉ bổ sung trường còn trống như nạp CSV."""
    res = res_of(request)
    try:
        outcome = await companies_service.create_company(
            res,
            body.name,
            tax_code=body.tax_code,
            website=body.website,
            note=body.note,
            enqueue=body.enqueue,
        )
    except companies_service.CompanyInputError as exc:
        raise HTTPException(400, str(exc)) from None
    await audit_service.record_safely(
        res.sessionmaker,
        action=audit_service.COMPANY_CREATE,
        actor=actor_of(request),
        resource_type="company",
        resource_id=outcome.id,
        metadata={
            "name": body.name,
            "created": outcome.created,
            "enqueue": body.enqueue,
            "queued": outcome.queued,
        },
    )
    return CompanyCreatedOut.model_validate(dataclasses.asdict(outcome))


@router.get("/companies/import/sample.csv")
async def get_import_sample() -> Response:
    """File CSV mẫu để tải về: 50 doanh nghiệp lớn của Việt Nam, đúng định dạng nạp danh sách."""
    return Response(
        companies_service.demo_csv(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="doanh-nghiep-mau.csv"'},
    )


@router.post("/companies/import")
async def post_import(
    request: Request,
    file: Annotated[UploadFile, File(description="companies.csv")],
    enqueue: Annotated[bool, Form()] = False,
) -> ImportOutcomeOut:
    res = res_of(request)
    try:
        outcome = await companies_service.import_csv(res, await file.read(), enqueue=enqueue)
    except batch_service.CsvFormatError as exc:
        raise HTTPException(400, str(exc)) from None
    await audit_service.record_safely(
        res.sessionmaker,
        action=audit_service.COMPANIES_IMPORT,
        actor=actor_of(request),
        metadata={
            "filename": file.filename,
            "rows": outcome.rows,
            "created": outcome.created,
            "updated": outcome.updated,
            "unchanged": outcome.unchanged,
            "duplicates": outcome.duplicates,
            "errors": len(outcome.errors),
            "enqueue": enqueue,
            "enqueued": outcome.enqueued,
            "already_queued": outcome.already_queued,
        },
    )
    return ImportOutcomeOut.model_validate(dataclasses.asdict(outcome))


@router.post("/enqueue")
async def post_enqueue(request: Request, body: EnqueueIn) -> EnqueueResultOut:
    if body.company_ids is None and body.stale_hours is None and not body.all:
        raise HTTPException(400, "Chọn company_ids, stale_hours hoặc all")
    res = res_of(request)
    result = await companies_service.enqueue_companies(
        res, company_ids=body.company_ids, stale_hours=body.stale_hours
    )
    ids = body.company_ids or []
    await audit_service.record_safely(
        res.sessionmaker,
        action=audit_service.COMPANIES_ENQUEUE,
        actor=actor_of(request),
        # Một doanh nghiệp: gắn vào doanh nghiệp đó để lịch sử của nó có dòng này.
        resource_type="company" if len(ids) == 1 else None,
        resource_id=ids[0] if len(ids) == 1 else None,
        metadata={
            **dataclasses.asdict(result),
            "all": body.company_ids is None,
            "stale_hours": body.stale_hours,
            "company_ids": audit_service.sample_ids(ids) if ids else None,
        },
    )
    return EnqueueResultOut.model_validate(dataclasses.asdict(result))
