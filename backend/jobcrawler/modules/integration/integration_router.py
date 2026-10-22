"""API cho hệ thống ngoài (`/api/integration/v1`, hướng dẫn: docs/INTEGRATION.md).

Mọi đường ở đây chỉ nhận khoá API (`Authorization: Bearer jca_…`, GuardMiddleware xác thực và
gắn `request.state.api_client`); quyền theo scope của client: `companies:write` (đẩy doanh
nghiệp, thu thập ngay), `jobs:read` (việc làm, feed thay đổi). Spec riêng cho bên ngoài ở
`GET /api/integration/v1/openapi.json`."""

from __future__ import annotations

import asyncio
import dataclasses
import time
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse

from jobcrawler.common.api import PageOut, actor_of, page_out, res_of
from jobcrawler.common.logging import get_logger
from jobcrawler.common.schemas import JobStatus
from jobcrawler.modules.audit import audit_service
from jobcrawler.modules.integration import feed_service, integration_service, refs_service
from jobcrawler.modules.integration.integration_schema import (
    CompanyBatchIn,
    CompanyBatchOut,
    CompanyImportOut,
    CompanyUpsertIn,
    CompanyUpsertOut,
    IntegrationCompanyOut,
    JobChangesOut,
    JobSnapshotOut,
    RefreshOut,
)
from jobcrawler.modules.integration.key_service import SCOPE_COMPANIES, SCOPE_JOBS, Principal
from jobcrawler.services.resources import Resources

log = get_logger(__name__)

PREFIX = "/api/integration/v1"
router = APIRouter(prefix=PREFIX, tags=["integration"])

RESYNC: dict[int | str, dict[str, Any]] = {
    410: {
        "description": "`resync_required`: cursor đã bị dọn (hoặc DB được khôi phục), "
        "đồng bộ lại từ snapshot `GET /jobs`"
    }
}
# Long-poll của feed: hỏi head trên Redis mỗi chừng này giây.
WAIT_STEP_SECONDS = 0.5
MAX_WAIT_SECONDS = 25  # dưới timeout đọc của nginx giao diện (120 s) và của client thường gặp
MAX_EXTERNAL_IDS = 100

SnapshotStatus = Literal["active", "all"]


def client_of(request: Request, scope: str | None = None) -> Principal:
    """Client của request (GuardMiddleware đã xác thực khoá); thiếu scope → 403."""
    principal = getattr(request.state, "api_client", None)
    if not isinstance(principal, Principal):
        raise HTTPException(401, "Cần khoá API")
    if scope is not None and scope not in principal.scopes:
        raise HTTPException(403, f"Khoá API không có quyền {scope}")
    return principal


def _scope(p: Principal) -> feed_service.Scope:
    return feed_service.Scope(p.client_id, p.all_companies, p.include_description)


def _batch_out(results: list[refs_service.RefResult]) -> dict[str, Any]:
    counts = {
        k: sum(r.result == k for r in results)
        for k in ("created", "linked", "updated", "unchanged")
    }
    return {
        "results": [CompanyUpsertOut.model_validate(dataclasses.asdict(r)) for r in results],
        **counts,
        "errors": sum(r.result == "error" for r in results),
    }


async def _audit_upsert(
    request: Request, res: Resources, results: list[refs_service.RefResult], how: str
) -> None:
    await audit_service.record_safely(
        res.sessionmaker,
        action=audit_service.INTEGRATION_COMPANIES,
        actor=actor_of(request),
        resource_type="api_client",
        resource_id=client_of(request).client_id,
        metadata={
            "via": how,
            "items": len(results),
            **{
                k: sum(r.result == k for r in results)
                for k in ("created", "linked", "updated", "error")
            },
        },
    )


# --- doanh nghiệp --------------------------------------------------------------------------------


@router.put("/companies/{external_id}")
async def put_integration_company(
    request: Request, external_id: str, body: CompanyUpsertIn
) -> CompanyUpsertOut:
    """Tạo / cập nhật một doanh nghiệp theo mã bên ngoài (idempotent). Doanh nghiệp mới được build
    ngay; website / trang tuyển dụng đổi thì build tìm nguồn lại."""
    p = client_of(request, SCOPE_COMPANIES)
    res = res_of(request)
    item = refs_service.clean_item(external_id, **body.model_dump())
    [result] = await refs_service.upsert(res, p.client_id, [item])
    if result.result == "error":
        raise HTTPException(422, result.error or "Doanh nghiệp không hợp lệ")
    await _audit_upsert(request, res, [result], "put")
    return CompanyUpsertOut.model_validate(dataclasses.asdict(result))


@router.post("/companies/batch")
async def post_integration_companies_batch(
    request: Request, body: CompanyBatchIn
) -> CompanyBatchOut:
    """Tối đa 1000 doanh nghiệp mỗi lần, kết quả từng mục theo đúng thứ tự (mục lỗi không chặn mục
    khác). Mã lặp lại trong lô: mục sau thắng."""
    p = client_of(request, SCOPE_COMPANIES)
    res = res_of(request)
    items = [
        refs_service.clean_item(i.external_id, **i.model_dump(exclude={"external_id"}))
        for i in body.items
    ]
    results = await refs_service.upsert(res, p.client_id, items)
    await _audit_upsert(request, res, results, "batch")
    return CompanyBatchOut.model_validate(_batch_out(results))


@router.post("/companies/import")
async def post_integration_companies_import(
    request: Request,
    file: Annotated[
        UploadFile,
        File(
            description="CSV UTF-8: external_id, name, tax_code, website, careers_url, note, active"
        ),
    ],
) -> CompanyImportOut:
    """Nạp file CSV (≤ 20 MB). Dòng hỏng được liệt kê ở `csv_errors`, không chặn dòng khác."""
    p = client_of(request, SCOPE_COMPANIES)
    res = res_of(request)
    try:
        report = integration_service.parse_csv(await file.read())
    except integration_service.CsvInputError as exc:
        raise HTTPException(400, str(exc)) from None
    done = await integration_service.import_csv(res, p.client_id, report)
    await _audit_upsert(request, res, done.results, "csv")
    return CompanyImportOut.model_validate(
        {
            **_batch_out(done.results),
            "rows": done.rows,
            "duplicates": done.duplicates,
            "csv_errors": done.errors,
            "csv_warnings": done.warnings,
        }
    )


@router.get("/companies")
async def get_integration_companies(
    request: Request, query: Annotated[integration_service.IntegrationCompanyQuery, Query()]
) -> PageOut[IntegrationCompanyOut]:
    """Doanh nghiệp đã đẩy vào, kèm trạng thái thu thập."""
    p = client_of(request)
    async with res_of(request).sessionmaker() as session:
        page = await integration_service.list_companies(session, p.client_id, query)
    return page_out(page, page.items)


@router.get("/companies/{external_id}")
async def get_integration_company(request: Request, external_id: str) -> IntegrationCompanyOut:
    """Trạng thái thu thập của một doanh nghiệp: KYC, lịch (new / ready / no_sources / broken /
    paused), số nguồn đang poll, số việc đang mở, lần build / poll gần nhất."""
    p = client_of(request)
    async with res_of(request).sessionmaker() as session:
        out = await integration_service.company_status(session, p.client_id, external_id)
    if out is None:
        raise HTTPException(404, "Không có doanh nghiệp với mã này")
    return out


@router.post("/companies/{external_id}/refresh")
async def post_integration_company_refresh(request: Request, external_id: str) -> RefreshOut:
    """Thu thập ngay một doanh nghiệp (poll nếu đã có bộ khung, không thì build). Tối đa một lần
    mỗi phút mỗi doanh nghiệp; doanh nghiệp đã tắt (`paused`) thì không chạy."""
    p = client_of(request, SCOPE_COMPANIES)
    res = res_of(request)
    async with res.sessionmaker() as session:
        status = await integration_service.company_status(session, p.client_id, external_id)
    if status is None:
        raise HTTPException(404, "Không có doanh nghiệp với mã này")
    kind, queued = await integration_service.refresh(res, status.company_id, status.crawl_state)
    await audit_service.record_safely(
        res.sessionmaker,
        action=audit_service.INTEGRATION_REFRESH,
        actor=actor_of(request),
        resource_type="company",
        resource_id=status.company_id,
        metadata={"external_id": external_id, "kind": kind, "queued": queued},
    )
    return RefreshOut(kind=kind, queued=queued)


# --- việc làm ------------------------------------------------------------------------------------


def _after(cursor: str | None) -> UUID | None:
    if not cursor:
        return None
    try:
        return UUID(cursor)
    except ValueError:
        raise HTTPException(400, "cursor không hợp lệ") from None


@router.get("/companies/{external_id}/jobs")
async def get_integration_company_jobs(
    request: Request,
    external_id: str,
    status: SnapshotStatus = "active",
    cursor: Annotated[str | None, Query(description="`next_cursor` của trang trước")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> JobSnapshotOut:
    """Việc làm của một doanh nghiệp (`active`: đang mở, chưa quá hạn nộp; `all`: mọi trạng thái),
    theo id tăng dần."""
    p = client_of(request, SCOPE_JOBS)
    async with res_of(request).sessionmaker() as session:
        ref = await refs_service.find(session, p.client_id, external_id)
        if ref is None:
            raise HTTPException(404, "Không có doanh nghiệp với mã này")
        return await feed_service.snapshot(
            session,
            _scope(p),
            company_ids=[ref.company_id],
            status=JobStatus.ACTIVE if status == "active" else None,
            after=_after(cursor),
            limit=limit,
            now=datetime.now(UTC),
        )


@router.get("/jobs")
async def get_integration_jobs(
    request: Request,
    external_id: Annotated[
        list[str],
        Query(max_length=MAX_EXTERNAL_IDS, description="Lọc theo mã doanh nghiệp (lặp lại)"),
    ] = [],  # noqa: B006 - FastAPI đọc mặc định, không sửa
    status: SnapshotStatus = "active",
    cursor: Annotated[str | None, Query(description="`next_cursor` của trang trước")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> JobSnapshotOut:
    """Snapshot việc làm của client (mọi doanh nghiệp đã đẩy, hoặc các mã `external_id`), theo id
    tăng dần. Trang đầu có `feed_cursor`: lấy hết snapshot rồi đọc `/jobs/changes` từ mốc đó."""
    p = client_of(request, SCOPE_JOBS)
    async with res_of(request).sessionmaker() as session:
        company_ids = None
        if external_id:
            company_ids = list(
                (await refs_service.company_ids(session, p.client_id, external_id)).values()
            )
        return await feed_service.snapshot(
            session,
            _scope(p),
            company_ids=company_ids,
            status=JobStatus.ACTIVE if status == "active" else None,
            after=_after(cursor),
            limit=limit,
            now=datetime.now(UTC),
        )


async def _wait_head(res: Resources, above: int, deadline: float) -> None:
    """Chờ relay báo seq mới (> `above`) trên Redis chính, tới `deadline` (monotonic)."""
    key = feed_service.head_key(res.settings.redis_prefix)
    while (left := deadline - time.monotonic()) > 0:
        try:
            raw = await res.redis.get(key)
            if raw is not None and int(raw) > above:
                return
        except Exception as exc:  # Redis lỗi: chờ hết hạn rồi đọc DB như thường
            log.warning("integration.head_unavailable", error=str(exc))
        await asyncio.sleep(min(WAIT_STEP_SECONDS, left))


@router.get("/jobs/changes", responses=RESYNC)
async def get_integration_job_changes(
    request: Request,
    cursor: Annotated[
        str | None, Query(description="`next_cursor` lần trước (hoặc `feed_cursor` của snapshot)")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
    wait: Annotated[
        int,
        Query(ge=0, le=MAX_WAIT_SECONDS, description="Long-poll: chờ tối đa N giây nếu chưa có"),
    ] = 0,
) -> JobChangesOut:
    """Feed thay đổi việc làm (created / updated / closed / reopened / deleted) sau `cursor`, theo
    seq tăng dần, kèm trạng thái hiện tại của việc. Giao ít nhất một lần: khử trùng theo `seq`,
    upsert theo `job.id`. Lưu `next_cursor` sau khi đã xử lý xong trang."""
    p = client_of(request, SCOPE_JOBS)
    res = res_of(request)
    try:
        start = feed_service.parse_cursor(cursor)
    except ValueError:
        raise HTTPException(400, "cursor không hợp lệ") from None
    deadline = time.monotonic() + wait
    while True:
        async with res.sessionmaker() as session:
            try:
                page = await feed_service.changes(
                    session, _scope(p), start, limit=limit, now=datetime.now(UTC)
                )
            except feed_service.ResyncRequired as exc:
                raise HTTPException(410, f"resync_required: {exc}") from None
        if page.events or time.monotonic() >= deadline:
            return page
        start = int(page.next_cursor)
        if not page.has_more:
            await _wait_head(res, int(page.head), deadline)


# --- spec cho hệ thống ngoài ------------------------------------------------------------------


def integration_spec(full: dict[str, Any]) -> dict[str, Any]:
    """Phần của spec đầy đủ chỉ gồm đường `/api/integration/v1` và các schema chúng dùng."""
    paths = {p: v for p, v in full["paths"].items() if p.startswith(PREFIX)}
    schemas: dict[str, Any] = full.get("components", {}).get("schemas", {})
    keep: set[str] = set()
    todo: list[Any] = [paths]
    while todo:
        node = todo.pop()
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                name = ref.rsplit("/", 1)[1]
                if name not in keep:
                    keep.add(name)
                    todo.append(schemas.get(name, {}))
            todo.extend(node.values())
        elif isinstance(node, list):
            todo.extend(node)
    return {
        "openapi": full["openapi"],
        "info": {
            "title": "jobcrawler — API tích hợp",
            "version": "1",
            "description": "Đẩy doanh nghiệp, đọc việc làm và feed thay đổi. Xác thực: "
            "`Authorization: Bearer jca_…`.",
        },
        "paths": paths,
        "components": {
            "schemas": {name: schemas[name] for name in sorted(keep) if name in schemas}
        },
        "tags": [t for t in full.get("tags", []) if t.get("name") == "integration"],
    }


@router.get("/openapi.json", include_in_schema=False)
async def get_integration_openapi(request: Request) -> JSONResponse:
    """Spec OpenAPI chỉ gồm các đường `/api/integration/v1` (cho đội tích hợp sinh client)."""
    spec = getattr(request.app.state, "integration_openapi", None)
    if spec is None:
        spec = integration_spec(request.app.openapi())
        request.app.state.integration_openapi = spec
    return JSONResponse(spec)
