"""Map profile nhà tuyển dụng trên trang tuyển dụng (`source_profiles`) ↔ doanh nghiệp đầu vào.

Chỉ xét profile có tên gần với alias của một doanh nghiệp (pg_trgm, một câu SQL cho cả lô) để
không tốn LLM cho hàng chục nghìn nhà tuyển dụng không liên quan. Quyết định:

1. Rule: MST trùng (1.0) → website trùng domain doanh nghiệp (0.95) → tên khớp sau chuẩn hoá với
   đúng một doanh nghiệp (0.85 nếu doanh nghiệp đã KYC xác minh, 0.8 nếu chưa). Mâu thuẫn MST hoặc
   website thì loại ứng viên đó.
2. Còn mơ hồ (nhiều ứng viên, tên chỉ gần giống) → LLM chọn trong danh sách, kèm lý do.

Kết quả (kể cả không map được) ghi `matched_at`: chỉ xét lại khi có alias mới (doanh nghiệp mới
được nạp/KYC sau lần thử trước).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from jobcrawler.db.models import Company, CompanyAlias, SourceProfile
from jobcrawler.llm import LLMClient
from jobcrawler.logging import get_logger
from jobcrawler.net.urls import parse_domain_input, registrable_domain
from jobcrawler.pipeline.company_facts import names_match
from jobcrawler.pipeline.kyc import trigram_compatible
from jobcrawler.prompts import profile_match
from jobcrawler.schemas import KycStatus
from jobcrawler.text import company_match_key

log = get_logger(__name__)

DEFAULT_TRIGRAM = 0.6
DEFAULT_MIN_CONFIDENCE = 0.8
MAX_CANDIDATES = 5
# Tên quá ngắn/chung chung thì khớp tên thôi chưa đủ (không tự map bằng rule).
MIN_NAME_CHARS = 6

Enrich = Callable[[SourceProfile], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Decision:
    company_id: UUID | None
    confidence: float
    evidence: dict[str, Any]


def _domains(company: Company) -> set[str]:
    found = {registrable_domain(d) for d in [company.primary_domain, *company.other_domains] if d}
    return {d for d in found if d}


def _profile_domain(profile: SourceProfile) -> str | None:
    if not profile.website:
        return None
    return registrable_domain(parse_domain_input(profile.website)) or None


def _company_names(company: Company) -> list[str]:
    return [n for n in (company.legal_name, company.brand_name, company.input_name) if n]


def decide(profile: SourceProfile, candidates: list[tuple[Company, str, float]]) -> Decision | None:
    """Quyết định bằng rule. None: còn mơ hồ (cần LLM). Decision(company_id=None): không khớp."""
    name_norm = profile.name_norm or ""
    domain = _profile_domain(profile)
    strong: dict[UUID, tuple[float, str]] = {}
    by_name: dict[UUID, Company] = {}
    plausible: dict[UUID, Company] = {}
    for company, alias_norm, _sim in candidates:
        if profile.tax_code and company.tax_code and profile.tax_code != company.tax_code:
            continue  # khác MST: chắc chắn khác pháp nhân
        domains = _domains(company)
        if company.tax_code and profile.tax_code == company.tax_code:
            strong[company.id] = (1.0, "mst")
            continue
        if domain and domain in domains:
            strong[company.id] = max(strong.get(company.id, (0, "")), (0.95, "website"))
            continue
        if domain and domains and company.kyc_status == KycStatus.VERIFIED.value:
            continue  # website khác domain đã xác minh của doanh nghiệp
        if not trigram_compatible(name_norm, alias_norm):
            continue
        plausible[company.id] = company
        if (
            name_norm == alias_norm
            or any(names_match(profile.name_on_source, n) for n in _company_names(company))
            or company_match_key(profile.name_on_source)
            in {company_match_key(n) for n in _company_names(company)}
        ):
            by_name[company.id] = company
    if len(strong) == 1:
        company_id, (confidence, signal) = next(iter(strong.items()))
        return Decision(company_id, confidence, {"signal": signal, "rule": True})
    if len(strong) > 1:
        return None
    if len(by_name) == 1 and len(name_norm.replace(" ", "")) >= MIN_NAME_CHARS:
        company = next(iter(by_name.values()))
        verified = company.kyc_status == KycStatus.VERIFIED.value
        if len(plausible) == 1:
            return Decision(company.id, 0.85 if verified else 0.8, {"signal": "name", "rule": True})
    if not plausible:
        return Decision(None, 0.0, {"signal": "no_compatible_candidate"})
    return None


def _render_profile(profile: SourceProfile) -> str:
    parts = [f"Tên: {profile.name_on_source}", f"Trang: {profile.url}"]
    if profile.website:
        parts.append(f"Website: {profile.website}")
    if profile.tax_code:
        parts.append(f"MST: {profile.tax_code}")
    for key in ("address", "industry", "size"):
        if value := (profile.info or {}).get(key):
            parts.append(f"{key}: {value}")
    return "\n".join(parts)


def _render_company(code: str, company: Company) -> str:
    fields = [
        f"[{code}] {company.legal_name or company.input_name}",
        f"thương hiệu: {company.brand_name}" if company.brand_name else "",
        f"tên đầu vào: {company.input_name}",
        f"MST: {company.tax_code}" if company.tax_code else "",
        f"website: {company.primary_domain}" if company.primary_domain else "",
        f"địa chỉ: {company.address}" if company.address else "",
        f"KYC: {company.kyc_status}" if company.kyc_status else "",
    ]
    return " · ".join(f for f in fields if f)


async def _ask_llm(
    llm: LLMClient,
    profile: SourceProfile,
    candidates: list[Company],
    *,
    min_confidence: float,
) -> Decision:
    codes = {f"C{i + 1}": c for i, c in enumerate(candidates)}
    result = await llm.structured(
        profile_match.PROMPT,
        profile_match.ProfileMatch,
        board=profile.source,
        profile=_render_profile(profile),
        companies="\n".join(_render_company(code, c) for code, c in codes.items()),
    )
    pick = result.value
    if pick is None:
        return Decision(None, 0.0, {"signal": "llm_unknown", "error": result.error})
    company = codes.get(pick.company or "")
    evidence = {"signal": "llm", "reason": pick.reason, "confidence": pick.confidence}
    if company is None or pick.confidence < min_confidence:
        return Decision(None, pick.confidence, evidence)
    return Decision(company.id, pick.confidence, evidence)


async def map_profiles(
    session: AsyncSession,
    *,
    source: str | None = None,
    llm: LLMClient | None = None,
    trigram_threshold: float = DEFAULT_TRIGRAM,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    enrich: Enrich | None = None,
    now: datetime | None = None,
) -> int:
    """Map các profile chưa map (hoặc đã thử trước khi có alias mới). Trả số profile map được."""
    now = now or datetime.now(UTC)
    latest_alias = select(func.max(CompanyAlias.created_at)).scalar_subquery()
    pending = [
        SourceProfile.company_id.is_(None),
        SourceProfile.name_norm.is_not(None),
        or_(SourceProfile.matched_at.is_(None), SourceProfile.matched_at < latest_alias),
    ]
    if source is not None:
        pending.append(SourceProfile.source == source)

    await session.execute(
        text("SELECT set_config('pg_trgm.similarity_threshold', :t, true)"),
        {"t": str(trigram_threshold)},
    )
    similarity = func.similarity(CompanyAlias.alias_norm, SourceProfile.name_norm)
    rows = (
        await session.execute(
            select(SourceProfile.id, CompanyAlias.company_id, CompanyAlias.alias_norm, similarity)
            .join(CompanyAlias, CompanyAlias.alias_norm.op("%")(SourceProfile.name_norm))
            .where(*pending)
            .order_by(SourceProfile.id, similarity.desc())
        )
    ).all()
    by_profile: dict[UUID, list[tuple[UUID, str, float]]] = {}
    for profile_id, company_id, alias_norm, sim in rows:
        if sim >= trigram_threshold:
            options = by_profile.setdefault(profile_id, [])
            if all(c != company_id for c, _, _ in options) and len(options) < MAX_CANDIDATES:
                options.append((company_id, alias_norm, sim))

    # Không có ứng viên: chỉ ghi đã thử.
    no_candidate = update(SourceProfile).where(*pending).values(matched_at=now)
    if by_profile:
        no_candidate = no_candidate.where(SourceProfile.id.not_in(list(by_profile)))
    await session.execute(no_candidate)
    if not by_profile:
        return 0

    profiles = {
        p.id: p
        for p in await session.scalars(
            select(SourceProfile).where(SourceProfile.id.in_(list(by_profile)))
        )
    }
    company_ids = {c for options in by_profile.values() for c, _, _ in options}
    companies = {
        c.id: c for c in await session.scalars(select(Company).where(Company.id.in_(company_ids)))
    }
    matched = 0
    for profile_id, options in by_profile.items():
        profile = profiles[profile_id]
        candidates = [(companies[c], alias, sim) for c, alias, sim in options if c in companies]
        decision = decide(profile, candidates)
        if decision is None and enrich is not None and not (profile.website or profile.tax_code):
            # Trang profile thường ghi website/MST: đọc thêm rồi quyết lại bằng rule.
            await enrich(profile)
            decision = decide(profile, candidates)
        if decision is None:
            uniq = list(dict.fromkeys(c for c, _, _ in candidates))
            if llm is not None and llm.enabled:
                decision = await _ask_llm(llm, profile, uniq, min_confidence=min_confidence)
            else:
                decision = Decision(None, 0.0, {"signal": "ambiguous_no_llm"})
        evidence = {
            **decision.evidence,
            "candidates": [
                {"company_id": str(c.id), "alias_norm": alias, "similarity": round(sim, 3)}
                for c, alias, sim in candidates
            ],
        }
        profile.matched_at = now
        profile.match_evidence = evidence
        if decision.company_id is not None and decision.confidence >= min_confidence:
            profile.company_id = decision.company_id
            profile.match_confidence = decision.confidence
            matched += 1
            log.info(
                "profile.matched",
                source=profile.source,
                profile=profile.name_on_source,
                company_id=decision.company_id,
                confidence=decision.confidence,
                signal=decision.evidence.get("signal"),
            )
    await session.flush()
    return matched
