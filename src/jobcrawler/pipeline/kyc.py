"""KYC doanh nghiệp: đúng pháp nhân, website chính thức, MST, tên pháp lý, alias (mục 6.1).

1. Tra cache (`resolve_company`): MST, `alias_norm` khớp chính xác, rồi trigram vượt ngưỡng
   (chỉ doanh nghiệp đã `verified`). `kyc_at` chưa quá KYC_CACHE_DAYS thì dùng luôn.
2. Không có (`run_kyc`): ứng viên từ SERP (website chính thức, trang tra cứu MST) và website
   (chân trang, JSON-LD Organization: tên pháp lý, MST, địa chỉ).
3. LLM chọn ứng viên theo mã (W1, R1...) kèm trích đoạn. Rule kiểm lại mọi trích đoạn/trường
   (phải có trong ứng viên) và tính tín hiệu khớp mạnh: cùng MST, cùng domain đầu vào, website
   tự ghi đúng tên, domain mang đúng tên, Knowledge Graph trỏ về website.
4. Bằng chứng yếu → `ambiguous`: lưu danh sách ứng viên; chỉ thu job từ domain khớp mạnh
   (domain đầu vào, hoặc trang ghi đúng MST đầu vào).
5. Lưu (`apply_kyc`): trường định danh, `primary_domain`, alias (đầu vào, pháp lý, viết tắt,
   thương hiệu, tiếng Anh). MST đã thuộc doanh nghiệp khác → gộp hai bản ghi.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import ValidationError
from rapidfuzz import fuzz
from sqlalchemy import delete, func, literal, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from jobcrawler.db.models import (
    Company,
    CompanyAlias,
    CompanyRun,
    Job,
    JobPosting,
    SourceFetch,
    SourceProfile,
)
from jobcrawler.llm import LLMClient
from jobcrawler.logging import get_logger
from jobcrawler.net.http import Fetcher
from jobcrawler.net.urls import (
    host_of,
    normalize_url,
    parse_domain_input,
    registrable_domain,
    same_site,
    site_root,
)
from jobcrawler.pages import extract_links, parse
from jobcrawler.pipeline import store
from jobcrawler.pipeline.company_facts import (
    Kind,
    SiteFacts,
    classify,
    contact_link,
    domain_matches_name,
    legal_name_lines,
    names_match,
    present,
    registry_tax_codes,
    scrub,
    shorten,
    site_facts,
    tax_codes_in,
    valid_tax_code,
)
from jobcrawler.prompts import kyc_pick
from jobcrawler.schemas import CompanyIn, JobStatus, KycCandidate, KycEvidence, KycRecord, KycStatus
from jobcrawler.serp import SerpClient, SerpItem, SerpPanel, SerpResponse
from jobcrawler.sources import ats
from jobcrawler.sources.base import FetchRecord, SourceError, SourceLimits, SourceSession
from jobcrawler.sources.careers import Page, fetch_page
from jobcrawler.text import company_match_key, fold, normalize_company_name

log = get_logger(__name__)

TIMEOUT = "timeout"
MAX_WEBSITES = 6  # ứng viên website đưa cho LLM
MAX_REGISTRY = 5
MAX_OTHER = 4
MAX_SERP_DOCS = 3  # kết quả tìm kiếm mỗi ứng viên website

# Tín hiệu khớp mạnh của website được chọn / của bản ghi đăng ký. `domain_name` (domain mang
# đúng tên) chỉ là tín hiệu phụ: nhiều doanh nghiệp trùng tên thương hiệu.
SITE_SIGNALS = frozenset({"input_domain", "tax_code", "site_name", "panel", "registry_tax_code"})
REGISTRY_SIGNALS = frozenset({"registry_tax_code_input", "registry_name"})
CONFLICTS = frozenset({"site_tax_code_mismatch", "tax_code_mismatch", "domain_mismatch"})
PAGE_EVIDENCE = frozenset({"llm", "tax_code", "site_name", "panel"})

# --- ứng viên ---------------------------------------------------------------------------------


@dataclass(slots=True)
class Doc:
    url: str
    label: str  # "Trang chủ", "Kết quả tìm kiếm #3", "Knowledge Graph"...
    text: str
    page: bool = False  # nội dung đọc từ chính website (không phải từ SERP)


@dataclass(slots=True)
class Candidate:
    id: str
    kind: Kind
    domain: str
    url: str
    title: str | None = None
    rank: int | None = None
    docs: list[Doc] = field(default_factory=list)
    facts: list[SiteFacts] = field(default_factory=list)
    tax_codes: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    _folded: str | None = None

    def add_tax_codes(self, codes: list[str]) -> None:
        for code in codes:
            if code not in self.tax_codes:
                self.tax_codes.append(code)

    def add_doc(self, doc: Doc) -> None:
        self.docs.append(doc)
        self._folded = None

    @property
    def folded(self) -> str:
        if self._folded is None:
            self._folded = fold("\n".join(d.text for d in self.docs))
        return self._folded

    @property
    def site_names(self) -> list[str]:
        return [n for f in self.facts for n in f.names]

    def find(self, value: str) -> Doc | None:
        """Tài liệu của ứng viên chứa `value` (đã kiểm `present`)."""
        for doc in self.docs:
            if present(value, fold(doc.text)):
                return doc
        return None

    def render(self) -> str:
        kinds = {"website": "website", "registry": "tra cứu doanh nghiệp", "other": "nguồn khác"}
        notes = []
        if "input" in self.flags:
            notes.append("người dùng cung cấp")
        if "panel" in self.flags:
            notes.append("Knowledge Graph trỏ tới")
        if self.rank is not None:
            notes.append(f"kết quả tìm kiếm #{self.rank}")
        head = f"[{self.id}] {kinds[self.kind]} {self.domain}"
        if notes:
            head += " (" + "; ".join(notes) + ")"
        body = "\n".join("  " + doc.text.replace("\n", "\n    ") for doc in self.docs)
        return f"{head}\n{body}" if body else head

    def summary(self) -> KycCandidate:
        snippet = next((d.text for d in self.docs if not d.page), None) or (
            self.docs[0].text if self.docs else None
        )
        return KycCandidate(
            id=self.id,
            kind=self.kind,
            domain=self.domain,
            url=self.url,
            title=self.title,
            snippet=scrub(shorten(snippet)) if snippet else None,
            tax_codes=self.tax_codes,
            rank=self.rank,
            flags=self.flags,
        )


class CandidateSet:
    def __init__(self, name: str) -> None:
        self.name = name
        self.websites: dict[str, Candidate] = {}  # theo domain đăng ký
        self.registry: dict[str, Candidate] = {}  # theo URL
        self.other: dict[str, Candidate] = {}

    def all(self) -> list[Candidate]:
        return [*self.websites.values(), *self.registry.values(), *self.other.values()]

    def by_id(self, cid: str | None) -> Candidate | None:
        if not cid:
            return None
        cid = cid.strip().strip("[]").upper()
        return next((c for c in self.all() if c.id == cid), None)

    def __bool__(self) -> bool:
        return bool(self.websites or self.registry)

    def website(self, domain: str, url: str, *, rank: int | None = None) -> Candidate:
        domain = registrable_domain(domain)
        found = self.websites.get(domain)
        if found is None:
            found = Candidate(
                id=f"W{len(self.websites) + 1}",
                kind="website",
                domain=domain,
                url=site_root(url),
                rank=rank,
            )
            self.websites[domain] = found
        elif rank is not None and (found.rank is None or rank < found.rank):
            found.rank = rank
        return found

    def add_input(self, website: str) -> Candidate:
        raw = website.strip()
        url = normalize_url(raw if "://" in raw else f"https://{raw}") or f"https://{raw}/"
        candidate = self.website(parse_domain_input(raw), url)
        candidate.flags.append("input")
        return candidate

    def add_serp(self, response: SerpResponse) -> None:
        if response.panel is not None:
            self._add_panel(response.panel)
        for item in response.items:
            self._add_item(item)

    def _add_panel(self, panel: SerpPanel) -> None:
        attrs = "; ".join(f"{k}: {v}" for k, v in panel.attributes.items())
        text = " — ".join(
            x for x in (panel.title, panel.kind, panel.website, panel.description, attrs) if x
        )
        url = panel.source_url or panel.website or ""
        doc = Doc(url=url, label="Knowledge Graph", text=f"Knowledge Graph: {text}")
        if panel.website:
            candidate = self.website(host_of(panel.website), panel.website)
            if "panel" not in candidate.flags:
                candidate.flags.append("panel")
                candidate.title = candidate.title or panel.title
                candidate.add_doc(doc)
        elif url and len(self.other) < MAX_OTHER and url not in self.other:
            self.other[url] = Candidate(
                id=f"C{len(self.other) + 1}",
                kind="other",
                domain=registrable_domain(host_of(url)),
                url=url,
                title=panel.title,
                docs=[doc],
            )

    def _add_item(self, item: SerpItem) -> None:
        host = host_of(item.url)
        domain = registrable_domain(host)
        kind = classify(domain, self.name)
        doc = Doc(
            url=item.url,
            label=f"Kết quả tìm kiếm #{item.rank}",
            text=f"Kết quả tìm kiếm {item.url} — {item.title} — {shorten(item.snippet)}",
        )
        if kind == "website":
            if domain not in self.websites and len(self.websites) >= MAX_WEBSITES:
                return
            candidate = self.website(domain, item.url, rank=item.rank)
            candidate.title = candidate.title or item.title
            if sum(not d.page for d in candidate.docs) < MAX_SERP_DOCS:
                candidate.add_doc(doc)
                # MST có nhãn trong snippet của chính website (vd. trang liên hệ).
                candidate.add_tax_codes(tax_codes_in(f"{item.title}\n{item.snippet}"))
            return
        bucket, prefix, limit = (
            (self.registry, "R", MAX_REGISTRY)
            if kind == "registry"
            else (self.other, "C", MAX_OTHER)
        )
        if item.url in bucket or len(bucket) >= limit:
            return
        candidate = Candidate(
            id=f"{prefix}{len(bucket) + 1}",
            kind=kind,
            domain=domain,
            url=item.url,
            title=item.title,
            rank=item.rank,
            docs=[doc],
        )
        if kind == "registry":
            candidate.add_tax_codes(registry_tax_codes(item.title, item.url, item.snippet))
        bucket[item.url] = candidate

    def to_fetch(self, limit: int) -> list[Candidate]:
        """Website cần mở: đầu vào, Knowledge Graph, rồi theo thứ hạng."""

        def order(c: Candidate) -> tuple[int, int]:
            if "input" in c.flags:
                return (0, 0)
            if "panel" in c.flags:
                return (1, 0)
            return (2, c.rank if c.rank is not None else 99)

        return sorted(self.websites.values(), key=order)[:limit]


def _www_variant(url: str) -> str | None:
    """Trang chủ ở host có/không có `www.`; None nếu URL không phải trang chủ."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.path not in ("", "/") or not host:
        return None
    other = host.removeprefix("www.") if host.startswith("www.") else f"www.{host}"
    return f"{parts.scheme}://{other}/"


async def _homepage(session: SourceSession, url: str) -> Page | None:
    """Trang chủ ở host có và không có `www.`, mở song song, lấy trang về trước: có site chỉ
    một biến thể chạy (vinamilk.com.vn trả 400; hoaphat.com.vn treo tới hết timeout)."""
    urls = [url, *([alt] if (alt := _www_variant(url)) else [])]
    errors: list[SourceError] = []

    async def one(target: str) -> Page | None:
        try:
            return await fetch_page(session, target)
        except SourceError as exc:
            errors.append(exc)
            return None

    tasks = [asyncio.ensure_future(one(u)) for u in urls]
    try:
        for done in asyncio.as_completed(tasks):
            if (page := await done) is not None:
                return page
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    if errors and len(errors) == len(urls):
        raise errors[0]  # mọi biến thể đều bị chặn
    return None


async def read_site(
    candidate: Candidate, session: SourceSession, *, contact_page: bool = True
) -> None:
    """Mở trang chủ (và trang liên hệ nếu trang chủ không ghi MST), gắn thông tin vào ứng viên.
    Lỗi/bị chặn chỉ làm mất ứng viên này."""
    try:
        page = await _homepage(session, candidate.url)
        if page is None:
            return
        facts = site_facts(page.html, page.url)
        pages = [("Trang chủ", facts)]
        if contact_page and not facts.tax_codes:
            url = contact_link(extract_links(parse(page.html), page.url), page.url)
            extra = await fetch_page(session, url, auto_render=False) if url else None
            if extra is not None:
                pages.append(("Trang liên hệ/giới thiệu", site_facts(extra.html, extra.url)))
    except SourceError as exc:
        log.info("kyc.site_error", domain=candidate.domain, error=str(exc))
        return
    if "fetched" not in candidate.flags:
        candidate.flags.append("fetched")
    for i, (label, page_facts) in enumerate(pages):
        candidate.facts.append(page_facts)
        candidate.add_tax_codes(page_facts.tax_codes)
        candidate.add_doc(
            Doc(url=page_facts.url, label=label, text=page_facts.describe(label), page=True)
        )
        if i == 0:
            candidate.title = candidate.title or page_facts.title


def render_candidates(candidates: CandidateSet) -> str:
    return "\n\n".join(c.render() for c in candidates.all())


# --- quyết định -------------------------------------------------------------------------------


@dataclass(slots=True)
class KycResult:
    status: KycStatus
    confidence: float = 0.0
    legal_name: str | None = None
    brand_name: str | None = None
    short_name: str | None = None
    english_name: str | None = None
    tax_code: str | None = None
    address: str | None = None
    primary_domain: str | None = None
    other_domains: list[str] = field(default_factory=list)
    # Domain được phép thu job (khớp mạnh: domain đầu vào, website đã xác minh, trang ghi đúng
    # MST đầu vào).
    strong_domains: list[str] = field(default_factory=list)
    evidence: list[KycEvidence] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    candidates: list[KycCandidate] = field(default_factory=list)
    picked: str | None = None
    reason: str | None = None
    serp: str | None = None
    llm: str | None = None
    error: str | None = None  # timeout: kết quả dở dang, không lưu
    duration_ms: int = 0

    def record(self) -> KycRecord:
        return KycRecord(
            evidence=self.evidence,
            signals=self.signals,
            candidates=self.candidates,
            picked=self.picked,
            reason=self.reason,
            serp=self.serp,
            llm=self.llm,
        )

    def aliases(self) -> list[tuple[str, str]]:
        """(kind, alias) cần lưu ngoài tên đầu vào; chỉ khi đã xác minh."""
        if self.status is not KycStatus.VERIFIED:
            return []
        pairs = [
            ("legal", self.legal_name),
            ("short", self.short_name),
            ("brand", self.brand_name),
            ("english", self.english_name),
        ]
        return [(kind, value) for kind, value in pairs if value]


@dataclass(slots=True)
class _Pick:
    """Lựa chọn của LLM sau khi kiểm bằng rule (mọi giá trị đều có trong ứng viên)."""

    website: Candidate | None = None
    registry: Candidate | None = None
    legal_name: str | None = None
    tax_code: str | None = None
    address: str | None = None
    short_name: str | None = None
    brand_name: str | None = None
    english_name: str | None = None
    evidence: list[KycEvidence] = field(default_factory=list)
    several: bool = False
    confidence: float = 0.0
    reason: str | None = None
    dropped: list[str] = field(default_factory=list)  # giá trị LLM đưa mà không có trong ứng viên


def validate_pick(pick: kyc_pick.KycPick, candidates: CandidateSet) -> _Pick:
    out = _Pick(several=pick.several_companies, reason=pick.reason)
    out.confidence = min(1.0, max(0.0, pick.confidence))
    website = candidates.by_id(pick.website)
    out.website = website if website is not None and website.kind == "website" else None
    registry = candidates.by_id(pick.registry)
    out.registry = registry if registry is not None and registry.kind == "registry" else None
    if pick.website and out.website is None:
        out.dropped.append(f"website={pick.website}")
    everything = " ".join(c.folded for c in candidates.all())

    tax = valid_tax_code(pick.tax_code)
    if tax is not None and f" {fold(tax)} " in f" {everything} ":
        out.tax_code = tax
    elif pick.tax_code:
        out.dropped.append(f"tax_code={pick.tax_code}")
    for name in ("legal_name", "address", "short_name", "brand_name", "english_name"):
        value = getattr(pick, name)
        value = " ".join(value.split()) if value else None
        if not value:
            continue
        if present(value, everything, fuzzy=name == "address"):
            setattr(out, name, value)
        else:
            out.dropped.append(f"{name}={value}")
    legal = out.legal_name
    if legal and normalize_company_name(legal) == fold(legal):
        # Không có loại hình pháp lý ("Masan Group"): là tên thương hiệu, không phải tên pháp lý.
        out.brand_name = out.brand_name or legal
        out.legal_name = None
        out.dropped.append(f"legal_name={legal} (không có loại hình pháp lý)")

    for quote in pick.evidence[:8]:
        candidate = candidates.by_id(quote.candidate)
        doc = candidate.find(quote.quote) if candidate is not None else None
        if doc is None or len(quote.quote.strip()) < 4:
            out.dropped.append(f"quote={quote.candidate}:{quote.quote[:60]}")
            continue
        out.evidence.append(
            KycEvidence(url=doc.url, snippet=scrub(shorten(quote.quote)), kind="llm")
        )
    return out


def _site_signals(
    site: Candidate, company_in: CompanyIn, input_domain: str | None, panel_title: str | None
) -> tuple[list[str], list[KycEvidence]]:
    signals: list[str] = []
    evidence: list[KycEvidence] = []
    name = company_in.name
    if input_domain and same_site(site.domain, input_domain):
        signals.append("input_domain")
        evidence.append(
            KycEvidence(url=site.url, snippet="Website do người dùng cung cấp", kind="input")
        )
    if company_in.tax_code and site.tax_codes:
        if company_in.tax_code in site.tax_codes:
            signals.append("tax_code")
            for facts in site.facts:
                if company_in.tax_code in facts.tax_codes:
                    evidence.append(
                        KycEvidence(
                            url=facts.url,
                            snippet=_context(facts.text(), company_in.tax_code),
                            kind="tax_code",
                        )
                    )
                    break
        else:
            signals.append("site_tax_code_mismatch")
    for facts in site.facts:
        matched = next((n for n in facts.names if names_match(n, name)), None)
        if matched:
            signals.append("site_name")
            evidence.append(KycEvidence(url=facts.url, snippet=shorten(matched), kind="site_name"))
            break
    if domain_matches_name(site.domain, name):
        signals.append("domain_name")
        evidence.append(
            KycEvidence(
                url=site.url, snippet=f"Domain {site.domain} mang tên {name}", kind="domain_name"
            )
        )
    if "panel" in site.flags and panel_title and names_match(panel_title, name):
        signals.append("panel")
        doc = next((d for d in site.docs if d.label == "Knowledge Graph"), None)
        if doc is not None:
            evidence.append(KycEvidence(url=doc.url, snippet=shorten(doc.text), kind="panel"))
    return signals, evidence


def _context(text: str, needle: str, width: int = 80) -> str:
    flat = " ".join(text.split())
    i = flat.find(needle)
    if i < 0:
        return shorten(flat)
    return scrub(flat[max(0, i - width) : i + len(needle) + width].strip())


def decide(
    company_in: CompanyIn,
    candidates: CandidateSet,
    pick: _Pick | None,
    *,
    panel_title: str | None = None,
    min_confidence: float = 0.8,
) -> KycResult:
    """Ghép lựa chọn của LLM (hoặc rule khi không có LLM) với tín hiệu khớp mạnh."""
    input_domain = _input_domain(company_in)
    result = KycResult(status=KycStatus.NOT_FOUND)
    result.candidates = [c.summary() for c in candidates.all()]

    if pick is None:
        _decide_by_rules(result, company_in, candidates, input_domain, panel_title)
    else:
        _decide_with_pick(
            result, company_in, candidates, pick, input_domain, panel_title, min_confidence
        )

    # Domain được thu job: đầu vào, website đã xác minh, trang ghi đúng MST đầu vào.
    strong: list[str] = []
    if input_domain:
        strong.append(registrable_domain(input_domain))
    if result.status is KycStatus.VERIFIED and result.primary_domain:
        strong.append(result.primary_domain)
    if company_in.tax_code:
        strong += [
            c.domain for c in candidates.websites.values() if company_in.tax_code in c.tax_codes
        ]
    result.strong_domains = list(dict.fromkeys(strong))
    if result.status is not KycStatus.VERIFIED:
        result.primary_domain = result.strong_domains[0] if result.strong_domains else None
    tax = result.tax_code
    if result.status is KycStatus.VERIFIED and tax:
        result.other_domains = [
            c.domain
            for c in candidates.websites.values()
            if tax in c.tax_codes and c.domain != result.primary_domain
        ]
    result.other_domains = [
        d
        for d in dict.fromkeys(result.other_domains + result.strong_domains)
        if d != result.primary_domain
    ]
    return result


def _input_domain(company_in: CompanyIn) -> str | None:
    """Domain website đầu vào; URL bảng ATS (greenhouse, lever...) không phải website DN."""
    if not company_in.website or ats.match_url(_as_url(company_in.website)) is not None:
        return None
    return parse_domain_input(company_in.website) or None


def _as_url(website: str) -> str:
    raw = website.strip()
    return raw if "://" in raw else f"https://{raw}"


def _decide_with_pick(
    result: KycResult,
    company_in: CompanyIn,
    candidates: CandidateSet,
    pick: _Pick,
    input_domain: str | None,
    panel_title: str | None,
    min_confidence: float,
) -> None:
    site, reg = pick.website, pick.registry
    result.confidence = pick.confidence
    result.reason = pick.reason
    result.picked = site.id if site else (reg.id if reg else None)
    result.evidence = list(pick.evidence)
    for name in ("legal_name", "tax_code", "address", "short_name", "brand_name", "english_name"):
        setattr(result, name, getattr(pick, name))
    signals: list[str] = []
    if input_domain and (site is None or not same_site(site.domain, input_domain)):
        signals.append("domain_mismatch")  # LLM không xác nhận website người dùng đưa
    if site is not None:
        site_signals, site_evidence = _site_signals(site, company_in, input_domain, panel_title)
        signals += site_signals
        result.evidence += site_evidence
        if pick.tax_code and site.tax_codes and pick.tax_code not in site.tax_codes:
            signals.append("site_tax_code_mismatch")
        if reg is not None and set(reg.tax_codes) & set(site.tax_codes):
            signals.append("registry_tax_code")
        if not result.tax_code and len(site.tax_codes) == 1:
            result.tax_code = site.tax_codes[0]
    if reg is not None:
        if company_in.tax_code and company_in.tax_code in reg.tax_codes:
            signals.append("registry_tax_code_input")
        # Tên pháp lý đầy đủ trong bản ghi đăng ký trùng tên đầu vào (đầu vào là tên pháp lý).
        legal_input = normalize_company_name(company_in.name) != fold(company_in.name)
        if (
            legal_input
            and pick.legal_name
            and reg.find(pick.legal_name) is not None
            and names_match(pick.legal_name, company_in.name)
        ):
            signals.append("registry_name")
        if not result.tax_code and len(reg.tax_codes) == 1:
            result.tax_code = reg.tax_codes[0]
    if site is not None:
        _drop_unlinked_identity(result, signals, site, reg, company_in)
    if company_in.tax_code and result.tax_code and result.tax_code != company_in.tax_code:
        signals.append("tax_code_mismatch")
    result.signals = list(dict.fromkeys(signals))
    if site is not None:
        result.primary_domain = site.domain

    if site is None and reg is None:
        # LLM không thấy ứng viên nào đúng: không tìm thấy, trừ khi có nhiều DN trùng tên hoặc
        # người dùng đã đưa website (chưa xác nhận được).
        several = pick.several or bool(input_domain)
        result.status = KycStatus.AMBIGUOUS if several else KycStatus.NOT_FOUND
        return
    strong = set(result.signals) & (SITE_SIGNALS if site is not None else REGISTRY_SIGNALS)
    # Bằng chứng phải là trích đoạn có trên trang (không tính "website do người dùng đưa" hay
    # "domain mang tên").
    quoted = any(e.kind in PAGE_EVIDENCE for e in result.evidence)
    weak = (
        pick.several
        or pick.confidence < min_confidence
        or not strong
        or bool(set(result.signals) & CONFLICTS)
        or not quoted
    )
    result.status = KycStatus.AMBIGUOUS if weak else KycStatus.VERIFIED


def _drop_unlinked_identity(
    result: KycResult,
    signals: list[str],
    site: Candidate,
    reg: Candidate | None,
    company_in: CompanyIn,
) -> None:
    """MST/tên pháp lý chỉ giữ khi gắn được với website đã chọn: có trên chính website, hoặc
    lấy từ bản ghi đăng ký có cùng MST với website / với đầu vào. Tránh gán MST của một doanh
    nghiệp trùng tên (và sau đó gộp nhầm theo MST)."""
    legal = result.legal_name
    linked = reg is not None and bool(
        set(reg.tax_codes) & set(site.tax_codes)
        or (company_in.tax_code and company_in.tax_code in reg.tax_codes)
        or (legal and site.find(legal) is not None and reg.find(legal) is not None)
    )
    allowed = set(site.tax_codes) | ({company_in.tax_code} if company_in.tax_code else set())
    if linked and reg is not None:
        allowed |= set(reg.tax_codes)
    if result.tax_code and result.tax_code not in allowed:
        signals.append("tax_code_unlinked")
        result.tax_code = None
    if result.legal_name and not linked and site.find(result.legal_name) is None:
        signals.append("legal_name_unlinked")
        result.legal_name = None


def _decide_by_rules(
    result: KycResult,
    company_in: CompanyIn,
    candidates: CandidateSet,
    input_domain: str | None,
    panel_title: str | None,
) -> None:
    """Không có LLM (tắt hoặc lỗi): chỉ xác minh khi đúng một website tự khẳng định danh tính
    (ghi đúng MST đầu vào hoặc ghi đúng tên), không mâu thuẫn."""
    scored: list[tuple[Candidate, list[str], list[KycEvidence]]] = []
    for site in candidates.websites.values():
        if "fetched" not in site.flags and "input" not in site.flags:
            continue
        signals, evidence = _site_signals(site, company_in, input_domain, panel_title)
        scored.append((site, signals, evidence))
    self_identified = [
        s
        for s in scored
        if {"tax_code", "site_name"} & set(s[1])
        and not set(s[1]) & CONFLICTS
        and (not input_domain or "input_domain" in s[1])
    ]
    if len(self_identified) == 1:
        site, signals, evidence = self_identified[0]
        result.status = KycStatus.VERIFIED
        result.confidence = 0.9 if "tax_code" in signals else 0.85
        result.primary_domain = site.domain
        result.picked = site.id
        result.signals = signals
        result.evidence = evidence
        if len(site.tax_codes) == 1:
            result.tax_code = site.tax_codes[0]
        org = next((f.org for f in site.facts if f.org), {})
        result.legal_name = org.get("legalName")
        result.address = org.get("address")
        result.reason = "rule: website tự ghi đúng " + ("MST" if "tax_code" in signals else "tên")
        return
    result.status = KycStatus.AMBIGUOUS if candidates or input_domain else KycStatus.NOT_FOUND
    result.reason = (
        "rule: nhiều website tự khẳng định danh tính"
        if self_identified
        else "rule: không website nào tự khẳng định danh tính"
        if candidates
        else "không có ứng viên"
    )


# --- chạy KYC ---------------------------------------------------------------------------------


def serp_queries(company_in: CompanyIn) -> list[str]:
    """Truy vấn 1: tên. Truy vấn 2 (khi cần): MST, hoặc tên + "mã số thuế"."""
    second = company_in.tax_code or f"{company_in.name} mã số thuế"
    return [company_in.name, second]


def _needs_second_query(company_in: CompanyIn, first: SerpResponse, cands: CandidateSet) -> bool:
    if company_in.tax_code:
        blob = " ".join(f"{i.title} {i.snippet}" for i in first.items)
        return company_in.tax_code not in blob
    return not cands.registry


async def run_kyc(
    company_in: CompanyIn,
    *,
    fetcher: Fetcher,
    llm: LLMClient,
    serp: SerpClient,
    timeout: float = 45,
    min_confidence: float = 0.8,
    max_sites: int = 3,
    records: list[FetchRecord] | None = None,
) -> KycResult:
    """KYC cho một doanh nghiệp (không đụng DB). Không ném lỗi mạng/LLM: thiếu dữ liệu thì
    `ambiguous`/`not_found`; hết `timeout` thì `error="timeout"` (kết quả dở, không nên lưu)."""
    started = time.monotonic()
    deadline = started + timeout
    records = records if records is not None else []
    cands = CandidateSet(company_in.name)
    input_domain = _input_domain(company_in)
    limits = SourceLimits(max_rendered_pages=1)
    serp_used: str | None = None
    panel_title: str | None = None
    error: str | None = None

    def session(domain: str) -> SourceSession:
        return SourceSession(f"kyc:{domain}", fetcher, llm, limits, records)

    async def gather() -> None:
        nonlocal serp_used, panel_title
        need_serp = True
        if input_domain:
            site = cands.add_input(company_in.website or input_domain)
            await read_site(site, session(site.domain))
            # Website đầu vào đã tự ghi MST/tên pháp lý: không cần tốn credit SERP (trừ khi có
            # MST đầu vào mà website không ghi MST đó: tra MST để đối chiếu).
            identified = any(
                f.tax_codes or f.org.get("legalName") or legal_name_lines(f.footer)
                for f in site.facts
            )
            unconfirmed_tax = (
                bool(company_in.tax_code) and company_in.tax_code not in site.tax_codes
            )
            need_serp = not identified or unconfirmed_tax
        if need_serp and not serp.enabled:
            log.warning("kyc.serp_disabled", reason="thiếu SERP_API_KEY: bỏ qua bước tìm kiếm")
        elif need_serp:
            first_q, second_q = serp_queries(company_in)
            first = await serp.search(first_q, records=records)
            if first.ok:
                serp_used = serp.provider
                cands.add_serp(first)
                panel_title = first.panel.title if first.panel else None
            if _needs_second_query(company_in, first, cands):
                second = await serp.search(second_q, records=records)
                if second.ok:
                    serp_used = serp.provider
                    cands.add_serp(second)
            sites = [c for c in cands.to_fetch(max_sites) if "fetched" not in c.flags]
            await asyncio.gather(*(read_site(c, session(c.domain)) for c in sites))

    # Phần thu ứng viên dừng sớm để còn thời gian cho LLM.
    gather_budget = max(timeout * 0.5, timeout - 25)
    try:
        async with asyncio.timeout(gather_budget):
            await gather()
    except TimeoutError:
        error = TIMEOUT
        log.warning("kyc.timeout", phase="gather", seconds=gather_budget)

    pick: _Pick | None = None
    llm_name: str | None = None
    if cands and llm.enabled:
        try:
            async with asyncio.timeout(max(1.0, deadline - time.monotonic())):
                answer = await llm.structured(
                    kyc_pick.PROMPT,
                    kyc_pick.KycPick,
                    name=company_in.name,
                    tax_code=company_in.tax_code or "(không có)",
                    website=company_in.website or "(không có)",
                    candidates=render_candidates(cands),
                )
        except TimeoutError:
            error = TIMEOUT
            log.warning("kyc.timeout", phase="llm")
        else:
            if answer.value is not None:
                pick = validate_pick(answer.value, cands)
                llm_name = f"{kyc_pick.PROMPT.name}@{kyc_pick.PROMPT.version}"
                log.info(
                    "kyc.pick",
                    website=answer.value.website,
                    registry=answer.value.registry,
                    confidence=answer.value.confidence,
                    several=answer.value.several_companies,
                    dropped=pick.dropped[:10],
                )

    result = decide(company_in, cands, pick, panel_title=panel_title, min_confidence=min_confidence)
    result.serp = serp_used
    result.llm = llm_name
    result.error = error
    result.duration_ms = int((time.monotonic() - started) * 1000)
    return result


# --- cache + lưu (Postgres) -------------------------------------------------------------------


@dataclass(slots=True)
class Resolved:
    company: Company
    how: str | None  # tax_code | alias | trigram | None (mới tạo)
    fresh: bool  # KYC còn hạn, dùng luôn


def is_compatible(company: Company, company_in: CompanyIn) -> bool:
    """Bản ghi không mâu thuẫn với đầu vào (MST, website)."""
    if company_in.tax_code and company.tax_code and company.tax_code != company_in.tax_code:
        return False
    domain = _input_domain(company_in)
    if domain and company.primary_domain:
        known = [company.primary_domain, *(company.other_domains or [])]
        return any(same_site(domain, d) for d in known)
    return True


def is_fresh(
    company: Company,
    company_in: CompanyIn,
    *,
    now: datetime,
    max_age: timedelta,
    serp_enabled: bool,
) -> bool:
    if company.kyc_at is None or company.kyc_status is None or now - company.kyc_at > max_age:
        return False
    if not is_compatible(company, company_in):
        return False
    domain = _input_domain(company_in)
    if domain and not company.primary_domain:
        return False  # đầu vào nay có website mà lần trước chưa có
    # Kết quả chưa xác minh tìm khi chưa có SERP: có SERP rồi thì tìm lại.
    serp_before = isinstance(company.kyc_evidence, dict) and company.kyc_evidence.get("serp")
    return not (company.kyc_status != KycStatus.VERIFIED and serp_enabled and not serp_before)


def trigram_compatible(a: str, b: str) -> bool:
    """Chặn trigram khớp nhầm hai doanh nghiệp khác nhau có tên gần giống: số phải trùng
    ("xây dựng số 1" ≠ "xây dựng số 2", similarity 0.83) và tên viết liền gần như giống
    ("techcombank" ~ "techcom bank", "vinamilkk" ~ "vinamilk")."""
    if re.findall(r"\d+", a) != re.findall(r"\d+", b):
        return False
    return fuzz.ratio(a.replace(" ", ""), b.replace(" ", "")) >= 85


async def _lock(session: AsyncSession, key: str) -> None:
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(key))))


MATCH_ALIAS = "match"


def alias_rows(company_id: UUID, alias: str, kind: str) -> list[dict[str, Any]]:
    """Dòng `company_aliases` cho một tên: tên chuẩn hoá, và khoá map profile trên trang tuyển
    dụng (`kind="match"`) nếu khác — để SQL trigram khi map tìm được "Shopee Việt Nam" từ
    "Shopee". Alias `match` không dùng khi tra cache KYC / nạp danh sách."""
    norm = normalize_company_name(alias)
    if len(norm) < 2:
        return []
    rows = [{"company_id": company_id, "alias": alias, "alias_norm": norm, "kind": kind}]
    key = company_match_key(alias)
    if len(key) >= 2 and key != norm:
        rows.append(
            {"company_id": company_id, "alias": alias, "alias_norm": key, "kind": MATCH_ALIAS}
        )
    return rows


async def _add_alias(session: AsyncSession, company_id: UUID, alias: str, kind: str) -> None:
    rows = alias_rows(company_id, alias, kind)
    if not rows:
        return
    await session.execute(
        pg_insert(CompanyAlias)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["company_id", "alias_norm"])
    )


async def find_company(
    session: AsyncSession,
    company_in: CompanyIn,
    *,
    now: datetime,
    max_age: timedelta,
    trigram_threshold: float,
    serp_enabled: bool,
) -> tuple[Company, str] | None:
    """Tra cache: MST → `alias_norm` khớp chính xác → trigram (chỉ DN đã xác minh, KYC còn
    hạn, không mâu thuẫn MST/website với đầu vào)."""
    if company_in.tax_code:
        company = await session.scalar(
            select(Company).where(Company.tax_code == company_in.tax_code)
        )
        if company is not None:
            return company, "tax_code"
    alias_norm = normalize_company_name(company_in.name)
    exact = (
        await session.scalars(
            select(Company)
            .join(CompanyAlias, CompanyAlias.company_id == Company.id)
            .where(
                CompanyAlias.alias_norm == alias_norm,
                CompanyAlias.kind.is_distinct_from(MATCH_ALIAS),
            )
            .order_by(
                (Company.kyc_status == KycStatus.VERIFIED.value).desc().nulls_last(), Company.id
            )
        )
    ).unique()
    for company in exact:
        if is_compatible(company, company_in):
            return company, "alias"
    # `%` dùng index GIN theo pg_trgm.similarity_threshold (đặt riêng cho giao dịch này).
    await session.execute(
        text("SELECT set_config('pg_trgm.similarity_threshold', :t, true)"),
        {"t": str(trigram_threshold)},
    )
    similarity = func.similarity(CompanyAlias.alias_norm, alias_norm)
    rows = await session.execute(
        select(Company, CompanyAlias.alias_norm, similarity)
        .join(CompanyAlias, CompanyAlias.company_id == Company.id)
        .where(
            CompanyAlias.alias_norm.op("%")(alias_norm),
            CompanyAlias.kind.is_distinct_from(MATCH_ALIAS),
            Company.kyc_status == KycStatus.VERIFIED.value,
        )
        .order_by(similarity.desc(), Company.id)
        .limit(5)
    )
    for company, matched, sim in rows:
        if (
            sim >= trigram_threshold
            and trigram_compatible(alias_norm, matched)
            and is_fresh(company, company_in, now=now, max_age=max_age, serp_enabled=serp_enabled)
        ):
            log.info("kyc.trigram_hit", company_id=company.id, similarity=round(sim, 3))
            return company, "trigram"
    return None


async def resolve_company(
    session: AsyncSession,
    company_in: CompanyIn,
    *,
    now: datetime | None = None,
    max_age: timedelta = timedelta(days=30),
    trigram_threshold: float = 0.7,
    serp_enabled: bool = False,
) -> Resolved:
    """Tìm doanh nghiệp trong cache, chưa có thì tạo (kèm alias tên đầu vào)."""
    now = now or datetime.now(UTC)
    alias_norm = normalize_company_name(company_in.name)
    # Khoá theo tên/MST để hai lượt song song không tạo hai bản ghi.
    await _lock(session, alias_norm)
    if company_in.tax_code:
        await _lock(session, f"mst:{company_in.tax_code}")
    found = await find_company(
        session,
        company_in,
        now=now,
        max_age=max_age,
        trigram_threshold=trigram_threshold,
        serp_enabled=serp_enabled,
    )
    if found is None:
        company = Company(input_name=company_in.name, tax_code=company_in.tax_code)
        session.add(company)
        await session.flush()
        how = None
    else:
        company, how = found
        if company_in.tax_code and not company.tax_code:
            company.tax_code = company_in.tax_code
    await _add_alias(session, company.id, company_in.name, "input")
    fresh = how is not None and is_fresh(
        company, company_in, now=now, max_age=max_age, serp_enabled=serp_enabled
    )
    return Resolved(company=company, how=how, fresh=fresh)


async def load_company(
    session: AsyncSession,
    company_id: UUID,
    company_in: CompanyIn,
    *,
    now: datetime | None = None,
    max_age: timedelta = timedelta(days=30),
    serp_enabled: bool = False,
) -> Resolved | None:
    """Doanh nghiệp theo id (đã `import`, job từ `enqueue`); None nếu bản ghi không còn (đã gộp
    vào doanh nghiệp khác khi KYC)."""
    company = await session.get(Company, company_id)
    if company is None:
        return None
    await _add_alias(session, company.id, company_in.name, "input")
    now = now or datetime.now(UTC)
    fresh = is_fresh(company, company_in, now=now, max_age=max_age, serp_enabled=serp_enabled)
    return Resolved(company=company, how="id", fresh=fresh)


async def merge_companies(session: AsyncSession, src: Company, dst: Company) -> None:
    """Gộp `src` vào `dst` (cùng pháp nhân): chuyển alias, lượt chạy, fetch, profile, job rồi
    xoá `src`."""
    await session.execute(
        pg_insert(CompanyAlias)
        .from_select(
            ["company_id", "alias", "alias_norm", "kind"],
            select(
                literal(dst.id),
                CompanyAlias.alias,
                CompanyAlias.alias_norm,
                CompanyAlias.kind,
            ).where(CompanyAlias.company_id == src.id),
        )
        .on_conflict_do_nothing(index_elements=["company_id", "alias_norm"])
    )
    for model in (CompanyRun, SourceFetch, SourceProfile, JobPosting):
        await session.execute(
            update(model).where(model.company_id == src.id).values(company_id=dst.id)
        )
    taken = select(Job.cluster_key).where(Job.company_id == dst.id)
    # Job đang mở của src y hệt một job đang mở của dst (cùng dấu vân tay) không chuyển sang —
    # bị xoá cùng src — để dst không có job trùng (index ux_jobs_company_fingerprint_active).
    same_content = select(Job.fingerprint).where(
        Job.company_id == dst.id,
        Job.status == JobStatus.ACTIVE.value,
        Job.fingerprint.is_not(None),
    )
    await session.execute(
        update(Job)
        .where(
            Job.company_id == src.id,
            Job.cluster_key.not_in(taken),
            (Job.status != JobStatus.ACTIVE.value)
            | Job.fingerprint.is_(None)
            | Job.fingerprint.not_in(same_content),
        )
        .values(company_id=dst.id)
    )
    domains = [dst.primary_domain, *(dst.other_domains or [])]
    extra = [src.primary_domain, *(src.other_domains or [])]
    dst.other_domains = [
        d for d in dict.fromkeys(x for x in domains + extra if x) if d != dst.primary_domain
    ]
    await session.execute(delete(Company).where(Company.id == src.id))
    session.expunge(src)
    await store.refresh_active_jobs(session, dst.id)
    log.info("kyc.merged", src_id=src.id, dst_id=dst.id)


async def apply_kyc(
    session: AsyncSession, company_id: UUID, result: KycResult, *, now: datetime | None = None
) -> Company:
    """Lưu kết quả KYC vào `companies` + `company_aliases`. Trả bản ghi cuối cùng (có thể là
    doanh nghiệp khác nếu MST đã thuộc về nó: hai bản ghi được gộp)."""
    now = now or datetime.now(UTC)
    company = await session.get(Company, company_id, with_for_update=True, populate_existing=True)
    if company is None:
        raise LookupError(f"không có company {company_id}")
    verified = result.status is KycStatus.VERIFIED
    if verified and result.tax_code and company.tax_code != result.tax_code:
        await _lock(session, f"mst:{result.tax_code}")
        owner = await session.scalar(
            select(Company)
            .where(Company.tax_code == result.tax_code, Company.id != company.id)
            .with_for_update()
        )
        if owner is not None:
            await merge_companies(session, company, owner)
            company = owner

    record = result.record()
    if not verified and company.kyc_status == KycStatus.VERIFIED.value:
        # Lần kiểm lại không xác nhận được (site lỗi, SERP đổi...): giữ kết quả đã xác minh.
        try:
            kept = KycRecord.model_validate(company.kyc_evidence or {})
        except ValidationError:
            kept = KycRecord()
        kept.last_check = {
            "at": now.isoformat(),
            "status": result.status.value,
            "reason": result.reason,
            "signals": result.signals,
        }
        company.kyc_evidence = kept.model_dump(mode="json")
        company.kyc_at = now
        await session.flush()
        return company

    company.kyc_status = result.status.value
    company.kyc_confidence = round(result.confidence, 3)
    company.kyc_evidence = record.model_dump(mode="json")
    company.kyc_at = now
    company.primary_domain = result.primary_domain
    company.other_domains = result.other_domains
    if verified:
        company.legal_name = result.legal_name or company.legal_name
        company.brand_name = result.brand_name or company.brand_name
        company.address = result.address or company.address
        if result.tax_code and not company.tax_code:
            company.tax_code = result.tax_code
    await session.flush()
    for kind, alias in result.aliases():
        await _add_alias(session, company.id, alias, kind)
    return company
