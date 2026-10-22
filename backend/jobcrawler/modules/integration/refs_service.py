"""Doanh nghiệp của hệ thống ngoài: (client, mã bên ngoài) → `companies` (`company_refs`).

`upsert` nhận từng doanh nghiệp (API từng cái, lô JSON, CSV) và trả kết quả theo đúng thứ tự:

- Mã đã có: cập nhật doanh nghiệp đang gắn (website / trang tuyển dụng / ghi chú mới đè, MST chỉ
  điền khi còn trống và không thuộc doanh nghiệp khác — lệch thì cảnh báo, giữ MST đã lưu).
  Website / trang tuyển dụng đổi thì xếp lượt build tìm nguồn lại.
- Mã mới: đi qua đúng đường nạp CSV (`batch_service._import_chunk`: khớp MST rồi tên chuẩn hoá,
  không tạo trùng) rồi gắn mã — `created` (doanh nghiệp mới) hoặc `linked` (đã có sẵn).
- `active=false` ở mọi mã của một doanh nghiệp → lịch `paused` (không build / poll); có lại một mã
  `active` → lịch `new` (build ngay).

Mỗi lô (`batch_service.IMPORT_CHUNK`) một transaction, khoá `ref:<client>:<mã>` theo thứ tự trước
khoá tên / MST của đường nạp CSV (cùng thứ tự với KYC nên không deadlock với lượt build; deadlock
với lượt KYC đang ghi MST thì chạy lại lô như `import_companies`).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from jobcrawler.common.logging import get_logger
from jobcrawler.common.schemas import CompanyIn, RunTrigger, ScheduleState
from jobcrawler.common.text import normalize_company_name
from jobcrawler.database.models import Company, CompanyAlias, CompanyRef, CompanySchedule
from jobcrawler.modules.companies import batch_service
from jobcrawler.pipeline import kyc
from jobcrawler.services.resources import Resources

log = get_logger(__name__)

RefOutcome = Literal["created", "linked", "updated", "unchanged", "error"]


@dataclass(slots=True)
class RefItem:
    """Một doanh nghiệp đã làm sạch, kèm mã bên ngoài."""

    external_id: str
    company: CompanyIn
    active: bool = True
    warnings: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RefResult:
    external_id: str
    result: RefOutcome
    company_id: UUID | None = None
    active: bool = True
    warnings: list[str] = field(default_factory=list)
    error: str | None = None


def clean_item(
    external_id: str,
    *,
    name: str,
    tax_code: str | None = None,
    website: str | None = None,
    careers_url: str | None = None,
    note: str | None = None,
    active: bool = True,
) -> RefItem | RefResult:
    """Làm sạch như một dòng CSV: MST / website / trang tuyển dụng sai thì bỏ trường đó (cảnh
    báo); mã hoặc tên không dùng được thì trả kết quả `error`."""
    external_id = external_id.strip()
    if not re.fullmatch(batch_service.EXTERNAL_ID_PATTERN, external_id):
        return RefResult(
            external_id, "error", error="mã không hợp lệ (chữ, số, . _ : @ -; ≤ 128 ký tự)"
        )
    tax, tax_warning = batch_service.clean_tax_code(tax_code)
    site, web_warning = batch_service.clean_website(website)
    careers, careers_warning = batch_service.clean_website(careers_url, label="careers_url")
    try:
        company = CompanyIn(name=name, tax_code=tax, website=site, careers_url=careers, note=note)
    except ValidationError:
        return RefResult(external_id, "error", active=active, error="thiếu tên doanh nghiệp")
    if len(normalize_company_name(company.name)) < 2:
        return RefResult(
            external_id, "error", active=active, error=f"tên {company.name!r} quá ngắn"
        )
    warnings = [w for w in (tax_warning, web_warning, careers_warning) if w]
    return RefItem(external_id, company, active, warnings)


async def upsert(
    res: Resources, client_id: UUID, items: Sequence[RefItem | RefResult]
) -> list[RefResult]:
    """Tạo / cập nhật doanh nghiệp và mã của client; kết quả cùng thứ tự `items` (mục đã là
    `RefResult` — lỗi lúc làm sạch — giữ nguyên). Mã trùng trong cùng lần gọi: mục sau thắng."""
    out: list[RefResult | None] = [i if isinstance(i, RefResult) else None for i in items]
    latest = {i.external_id: n for n, i in enumerate(items) if isinstance(i, RefItem)}
    todo = [
        (n, i) for n, i in enumerate(items) if isinstance(i, RefItem) and latest[i.external_id] == n
    ]
    rebuild: set[UUID] = set()
    for start in range(0, len(todo), batch_service.IMPORT_CHUNK):
        chunk = todo[start : start + batch_service.IMPORT_CHUNK]
        for attempt in range(batch_service.IMPORT_DEADLOCK_RETRIES + 1):
            try:
                async with res.sessionmaker.begin() as session:
                    done, changed = await _upsert_chunk(session, client_id, [i for _, i in chunk])
            except DBAPIError as exc:
                if (
                    attempt == batch_service.IMPORT_DEADLOCK_RETRIES
                    or not batch_service.is_deadlock(exc)
                ):
                    raise
                log.warning("integration.refs_deadlock_retry", attempt=attempt + 1)
                continue
            rebuild |= changed
            for (n, _), result in zip(chunk, done, strict=True):
                out[n] = result
            break
    # Mã lặp lại trong cùng lần gọi: mục trước nhận kết quả của mục sau (đã ghi).
    for n, item in enumerate(items):
        if out[n] is None and isinstance(item, RefItem):
            final = out[latest[item.external_id]]
            assert final is not None
            out[n] = replace(final, result="unchanged", warnings=["mã lặp lại, dùng mục sau"])
    if rebuild:
        from jobcrawler.modules.crawl import crawl_service

        await crawl_service.enqueue_builds(
            res, sorted(rebuild), trigger=RunTrigger.MANUAL, refresh=True
        )
    results = [r for r in out if r is not None]
    log.info(
        "integration.refs_upserted",
        client_id=str(client_id),
        items=len(results),
        errors=sum(r.result == "error" for r in results),
        rebuild=len(rebuild),
    )
    return results


async def _upsert_chunk(
    session: AsyncSession, client_id: UUID, items: Sequence[RefItem]
) -> tuple[list[RefResult], set[UUID]]:
    """Một lô (mã không trùng nhau) trong transaction của `session`. Trả kết quả và các doanh
    nghiệp đã gắn mã có website / trang tuyển dụng vừa đổi (cần build lại)."""
    for ext in sorted({i.external_id for i in items}):
        await kyc._lock(session, f"ref:{client_id}:{ext}")  # pyright: ignore[reportPrivateUsage]
    existing = {
        ref.external_id: (ref, company)
        for ref, company in await session.execute(
            select(CompanyRef, Company)
            .join(Company, Company.id == CompanyRef.company_id)
            .where(
                CompanyRef.client_id == client_id,
                CompanyRef.external_id.in_([i.external_id for i in items]),
            )
        )
    }
    results: dict[str, RefResult] = {}
    rebuild: set[UUID] = set()
    touched: set[UUID] = set()

    # Mã mới trước (khoá tên rồi MST như đường nạp CSV), mã đã có sau.
    fresh = [i for i in items if i.external_id not in existing]
    if fresh:
        part = batch_service.ImportResult()
        await batch_service._import_chunk(  # pyright: ignore[reportPrivateUsage]
            session, [i.company for i in fresh], part
        )
        now = datetime.now(UTC)
        for item, (company_id, outcome) in zip(fresh, part.rows, strict=True):
            results[item.external_id] = RefResult(
                item.external_id,
                "created" if outcome == "created" else "linked",
                company_id=company_id,
                active=item.active,
                warnings=list(item.warnings),
            )
            touched.add(company_id)
        await session.execute(
            pg_insert(CompanyRef).values(
                [
                    {
                        "client_id": client_id,
                        "external_id": i.external_id,
                        "company_id": results[i.external_id].company_id,
                        "active": i.active,
                        "updated_at": now,
                    }
                    for i in fresh
                ]
            )
        )

    for item in items:
        if item.external_id not in existing:
            continue
        ref, company = existing[item.external_id]
        row = item.company
        warnings = list(item.warnings)
        if row.tax_code and row.tax_code != company.tax_code:
            if company.tax_code:
                warnings.append(
                    f"MST {row.tax_code} khác MST đã lưu {company.tax_code}: giữ MST cũ"
                )
                row = row.model_copy(update={"tax_code": None})
            else:
                await kyc._lock(session, f"mst:{row.tax_code}")  # pyright: ignore[reportPrivateUsage]
                owner = await session.scalar(
                    select(Company.id).where(Company.tax_code == row.tax_code)
                )
                if owner is not None:
                    warnings.append(f"MST {row.tax_code} đã thuộc doanh nghiệp khác: bỏ qua")
                    row = row.model_copy(update={"tax_code": None})
        before = (company.input_website, company.input_careers_url)
        changed = batch_service.apply_input(company, row)
        if (company.input_website, company.input_careers_url) != before:
            rebuild.add(company.id)
        if normalize_company_name(row.name) != normalize_company_name(company.input_name):
            await session.execute(
                pg_insert(CompanyAlias)
                .values(kyc.alias_rows(company.id, row.name, "input"))
                .on_conflict_do_nothing(index_elements=["company_id", "alias_norm"])
            )
        if ref.active != item.active:
            ref.active = item.active
            changed = True
        results[item.external_id] = RefResult(
            item.external_id,
            "updated" if changed else "unchanged",
            company_id=company.id,
            active=item.active,
            warnings=warnings,
        )
        touched.add(company.id)

    await sync_pause(session, touched)
    return [results[i.external_id] for i in items], rebuild


async def sync_pause(session: AsyncSession, company_ids: set[UUID]) -> None:
    """Doanh nghiệp có mã mà mọi mã `active=false` → lịch `paused`; có mã `active` mà đang
    `paused` → `new` (build ngay)."""
    if not company_ids:
        return
    now = datetime.now(UTC)
    rows = await session.execute(
        select(CompanyRef.company_id, func.bool_or(CompanyRef.active))
        .where(CompanyRef.company_id.in_(company_ids))
        .group_by(CompanyRef.company_id)
    )
    for company_id, any_active in rows.all():
        if any_active:
            await session.execute(
                update(CompanySchedule)
                .where(
                    CompanySchedule.company_id == company_id,
                    CompanySchedule.state == ScheduleState.PAUSED.value,
                )
                .values(state=ScheduleState.NEW.value, next_build_at=now, updated_at=now)
            )
        else:
            stmt = pg_insert(CompanySchedule).values(
                company_id=company_id, state=ScheduleState.PAUSED.value
            )
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[CompanySchedule.company_id],
                    set_={"state": ScheduleState.PAUSED.value, "updated_at": now},
                )
            )


async def find(session: AsyncSession, client_id: UUID, external_id: str) -> CompanyRef | None:
    return await session.get(CompanyRef, (client_id, external_id))


async def company_ids(
    session: AsyncSession, client_id: UUID, external_ids: Sequence[str]
) -> dict[str, UUID]:
    """Mã bên ngoài → doanh nghiệp, chỉ các mã client đã có."""
    rows = await session.execute(
        select(CompanyRef.external_id, CompanyRef.company_id).where(
            CompanyRef.client_id == client_id, CompanyRef.external_id.in_(list(external_ids))
        )
    )
    return {ext: cid for ext, cid in rows.all()}
