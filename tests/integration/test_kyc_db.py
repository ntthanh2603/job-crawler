"""KYC với Postgres thật: cache alias (khớp chính xác, trigram), lưu kết quả, gộp theo MST."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from jobcrawler.config import Settings
from jobcrawler.db.models import Company, CompanyAlias, CompanyRun, SourceFetch
from jobcrawler.db.session import create_engine, create_sessionmaker
from jobcrawler.fakes import FakeChatClient, FakeFetcher, fetch_result
from jobcrawler.llm import LLMClient
from jobcrawler.logging import configure_logging
from jobcrawler.net.http import FetchResult
from jobcrawler.pipeline import kyc
from jobcrawler.resources import open_resources
from jobcrawler.schemas import CompanyIn, KycEvidence, KycStatus, RunStatus
from jobcrawler.serp import SERPER_URL
from jobcrawler.services.companies import process_company

FIXTURES = Path(__file__).parent.parent / "fixtures"

pytestmark = pytest.mark.integration


@pytest.fixture
async def sessions(
    integration_settings: Settings,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(integration_settings)
    try:
        yield create_sessionmaker(engine)
    finally:
        await engine.dispose()


def verified(**fields: Any) -> kyc.KycResult:
    result = kyc.KycResult(status=KycStatus.VERIFIED, confidence=0.95, **fields)
    result.evidence = [KycEvidence(url="https://example.vn/", snippet="MST: x", kind="llm")]
    result.serp = "serper"
    return result


async def resolve(
    sessions: async_sessionmaker[AsyncSession], company_in: CompanyIn, **kwargs: Any
) -> kyc.Resolved:
    async with sessions.begin() as session:
        return await kyc.resolve_company(session, company_in, trigram_threshold=0.7, **kwargs)


async def aliases(sessions: async_sessionmaker[AsyncSession], company_id: int) -> set[tuple]:
    async with sessions() as session:
        rows = await session.execute(
            select(CompanyAlias.kind, CompanyAlias.alias_norm).where(
                CompanyAlias.company_id == company_id
            )
        )
        return {tuple(r) for r in rows}


async def test_cache_exact_trigram_and_freshness(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    first = await resolve(sessions, CompanyIn(name="Công ty CP Sữa Kiểm Thử"))
    assert (first.how, first.fresh) == (None, False)
    company_id = first.company.id

    result = verified(
        legal_name="CÔNG TY CỔ PHẦN SỮA KIỂM THỬ",
        brand_name="Kiemthumilk",
        english_name="Kiemthu Dairy JSC",
        tax_code="0300000001",
        address="1 Đường Thử, Hà Nội",
        primary_domain="kiemthumilk.vn",
    )
    async with sessions.begin() as session:
        company = await kyc.apply_kyc(session, company_id, result)
    assert company.id == company_id
    assert (company.kyc_status, company.tax_code, company.primary_domain) == (
        "verified",
        "0300000001",
        "kiemthumilk.vn",
    )
    assert isinstance(company.kyc_evidence, dict)
    assert company.kyc_evidence["serp"] == "serper"
    assert await aliases(sessions, company_id) == {
        ("input", "sua kiem thu"),
        ("brand", "kiemthumilk"),
        ("english", "kiemthu dairy"),
    }  # tên pháp lý trùng alias_norm với tên đầu vào: không lưu hai lần

    brand = await resolve(sessions, CompanyIn(name="KIEMTHUMILK"))
    assert (brand.company.id, brand.how, brand.fresh) == (company_id, "alias", True)
    by_tax = await resolve(sessions, CompanyIn(name="Tên khác hẳn", tax_code="0300000001"))
    assert (by_tax.company.id, by_tax.how) == (company_id, "tax_code")

    typo = await resolve(sessions, CompanyIn(name="Kiemthumilkk"))
    assert (typo.company.id, typo.how, typo.fresh) == (company_id, "trigram", True)
    assert ("input", "kiemthumilkk") in await aliases(sessions, company_id)

    # Mâu thuẫn MST / website với bản ghi → không dùng cache, tạo doanh nghiệp mới.
    other_tax = await resolve(sessions, CompanyIn(name="Kiemthumilk", tax_code="0399999999"))
    assert other_tax.company.id != company_id
    other_site = await resolve(sessions, CompanyIn(name="Kiemthumilk", website="khac.vn"))
    assert other_site.company.id != company_id
    # (bản ghi vừa tạo theo MST khác chưa có website nên không mâu thuẫn, được dùng lại)
    assert (other_site.company.id, other_site.fresh) == (other_tax.company.id, False)

    # Website đầu vào trùng domain đã xác minh: vẫn dùng cache.
    same_site = await resolve(sessions, CompanyIn(name="Kiemthumilk", website="www.kiemthumilk.vn"))
    assert (same_site.company.id, same_site.fresh) == (company_id, True)

    # Quá 30 ngày: khớp chính xác vẫn ra bản ghi nhưng phải KYC lại; trigram thì bỏ qua.
    async with sessions.begin() as session:
        stale = await session.get(Company, company_id)
        assert stale is not None
        stale.kyc_at = datetime.now(UTC) - timedelta(days=31)
    exact_stale = await resolve(sessions, CompanyIn(name="Kiemthumilk"))
    assert (exact_stale.company.id, exact_stale.fresh) == (company_id, False)
    trigram_stale = await resolve(sessions, CompanyIn(name="Kiemthumilkkk"))
    assert trigram_stale.company.id != company_id


async def test_trigram_does_not_join_numbered_companies(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    one = await resolve(sessions, CompanyIn(name="Công ty CP Xây dựng Thử số 1 Hà Nội"))
    async with sessions.begin() as session:
        await kyc.apply_kyc(session, one.company.id, verified(primary_domain="xdthu1.vn"))
    two = await resolve(sessions, CompanyIn(name="Công ty CP Xây dựng Thử số 2 Hà Nội"))
    assert two.company.id != one.company.id  # similarity 0.83 nhưng khác số


async def test_unverified_result_without_serp_is_redone_once_serp_enabled(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    created = await resolve(sessions, CompanyIn(name="Công ty TNHH Chưa Tìm Thấy"))
    async with sessions.begin() as session:
        await kyc.apply_kyc(session, created.company.id, kyc.KycResult(status=KycStatus.NOT_FOUND))
    again = await resolve(sessions, CompanyIn(name="Chưa Tìm Thấy"))
    assert again.fresh
    with_serp = await resolve(sessions, CompanyIn(name="Chưa Tìm Thấy"), serp_enabled=True)
    assert not with_serp.fresh


async def test_tax_code_owner_absorbs_duplicate(sessions: async_sessionmaker[AsyncSession]) -> None:
    owner = await resolve(sessions, CompanyIn(name="Tập đoàn Gộp Thử", tax_code="0300000002"))
    dup = await resolve(sessions, CompanyIn(name="GopThu"))
    run_id = __import__("uuid").uuid4()
    async with sessions.begin() as session:
        session.add(
            CompanyRun(run_id=run_id, company_id=dup.company.id, started_at=datetime.now(UTC))
        )

    result = verified(
        legal_name="Tập đoàn Gộp Thử", tax_code="0300000002", primary_domain="gopthu.vn"
    )
    async with sessions.begin() as session:
        merged = await kyc.apply_kyc(session, dup.company.id, result)

    assert merged.id == owner.company.id
    assert merged.primary_domain == "gopthu.vn"
    async with sessions() as session:
        assert await session.get(Company, dup.company.id) is None
        run = await session.get(CompanyRun, run_id)
        assert run is not None
        assert run.company_id == owner.company.id
    assert ("input", "gopthu") in await aliases(sessions, owner.company.id)


async def test_failed_recheck_keeps_verified(sessions: async_sessionmaker[AsyncSession]) -> None:
    created = await resolve(sessions, CompanyIn(name="Công ty TNHH Giữ Nguyên"))
    async with sessions.begin() as session:
        await kyc.apply_kyc(
            session,
            created.company.id,
            verified(legal_name="Công ty TNHH Giữ Nguyên", primary_domain="giunguyen.vn"),
        )
    recheck = kyc.KycResult(status=KycStatus.AMBIGUOUS, reason="site lỗi")
    async with sessions.begin() as session:
        company = await kyc.apply_kyc(session, created.company.id, recheck)
    assert company.kyc_status == "verified"
    assert company.primary_domain == "giunguyen.vn"
    assert isinstance(company.kyc_evidence, dict)
    assert company.kyc_evidence["last_check"]["status"] == "ambiguous"


async def test_process_company_runs_kyc_then_sources(integration_settings: Settings) -> None:
    """Chỉ có tên: SERP → website → footer → LLM chọn → verified → tìm nguồn trên domain đó.
    Lượt sau dùng cache KYC (không gọi SERP/LLM)."""
    settings = integration_settings.model_copy(
        update={
            "redis_prefix": "jc-test",
            "serp_api_key": SecretStr("test-key"),
            "serp_cache_seconds": 0,
            "discovery_cache_seconds": 0,
            "discovery_negative_cache_seconds": 0,
            "kyc_max_sites": 1,
        }
    )
    configure_logging(settings)
    queries: list[str] = []

    def serper(kwargs: dict[str, Any]) -> FetchResult:
        queries.append(kwargs["json"]["q"])
        payload = {
            "organic": [
                {"position": 1, "link": "https://www.vinamilk.com.vn/", "title": "Vinamilk"},
                {
                    "position": 2,
                    "link": "https://masothue.com/0300588569-cong-ty-co-phan-sua-viet-nam",
                    "title": "0300588569 - CÔNG TY CỔ PHẦN SỮA VIỆT NAM",
                },
            ]
        }
        return fetch_result(SERPER_URL, json.dumps(payload), content_type="application/json")

    fetcher = FakeFetcher()
    fetcher.add_route(SERPER_URL, serper)
    fetcher.add_file("https://www.vinamilk.com.vn/", FIXTURES / "kyc/vinamilk_home.html")
    answer = {
        "evidence": [
            {"candidate": "W1", "quote": "CÔNG TY CỔ PHẦN SỮA VIỆT NAM"},
            {"candidate": "R1", "quote": "0300588569 - CÔNG TY CỔ PHẦN SỮA VIỆT NAM"},
        ],
        "website": "W1",
        "registry": "R1",
        "legal_name": "CÔNG TY CỔ PHẦN SỮA VIỆT NAM",
        "tax_code": "0300588569",
        "address": None,
        "short_name": None,
        "brand_name": "Vinamilk",
        "english_name": None,
        "several_companies": False,
        "confidence": 0.95,
        "reason": "footer ghi đúng tên, MST",
    }
    chat = FakeChatClient([json.dumps(answer, ensure_ascii=False), "{}"])
    llm = LLMClient(chat, model="qwen")

    async with open_resources(settings, db_logs=False) as res:
        res = dataclasses.replace(res, http=fetcher, llm=llm)  # pyright: ignore[reportArgumentType]
        first = await process_company(res, CompanyIn(name="Vinamilk"))
        calls_after_first = len(chat.calls)
        second = await process_company(res, CompanyIn(name="VINAMILK JSC"))
        async with res.sessionmaker() as session:
            company = await session.get(Company, first.company_id)
            sources = set(
                await session.scalars(
                    select(SourceFetch.source).where(SourceFetch.run_id == first.run_id)
                )
            )

    assert first.kyc_status is KycStatus.VERIFIED
    assert first.website == "vinamilk.com.vn"
    assert first.status is RunStatus.OK
    assert first.company_name == "CÔNG TY CỔ PHẦN SỮA VIỆT NAM"
    assert company is not None
    assert (company.tax_code, company.primary_domain) == ("0300588569", "vinamilk.com.vn")
    assert {"serp:serper", "kyc:vinamilk.com.vn", "discovery:vinamilk.com.vn"} <= sources

    assert second.company_id == first.company_id
    assert second.kyc_status is KycStatus.VERIFIED
    assert queries == ["Vinamilk"]  # lượt sau dùng cache KYC
    assert not any(
        "kyc_pick" in json.dumps(c["response_format"]) for c in chat.calls[calls_after_first:]
    )
