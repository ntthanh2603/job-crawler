"""Xác minh tin đã chuẩn hoá: rule loại trước (mỗi rule một mã lý do), tin còn nghi ngờ thì LLM
chấm; mọi quyết định nhận/loại đều kèm lý do.

Mã loại: `EXPIRED`, `EMPTY_JD`, `EMPLOYER_MISMATCH`, `AGENCY_POSTING`, `SCAM_SIGNALS`,
`NOT_A_JOB`, `SPAM_REPOST`, và `LOW_CONFIDENCE` (còn nghi ngờ mà LLM không xác nhận được).

Độ tin cậy ban đầu theo nguồn và cách bóc (ATS API > JSON-LD > parser > heuristic > LLM). Tin có
điểm cần xem lại (tên nhà tuyển dụng khác, dấu hiệu lừa đảo nhẹ, bóc bằng LLM...) bị hạ dưới
ngưỡng và phải được LLM xác nhận (`confidence ≥ VERIFY_MIN_CONFIDENCE`, mặc định 0.8). Không có
LLM / hết giờ thì loại: ưu tiên chính xác hơn số lượng.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from rapidfuzz import fuzz

from jobcrawler.llm import LLMClient, cache_key, cached_structured
from jobcrawler.logging import get_logger
from jobcrawler.net.kv import KeyValueStore
from jobcrawler.net.urls import host_of, path_segments, registrable_domain
from jobcrawler.pipeline.dedup import Signatures, connected_groups, jd_similarity, title_similarity
from jobcrawler.pipeline.normalize import NormalizedJob
from jobcrawler.prompts import job_verify
from jobcrawler.schemas import ExtractionMethod
from jobcrawler.text import fold, normalize_company_name

log = get_logger(__name__)

EXPIRED = "EXPIRED"
EMPTY_JD = "EMPTY_JD"
EMPLOYER_MISMATCH = "EMPLOYER_MISMATCH"
AGENCY_POSTING = "AGENCY_POSTING"
SCAM_SIGNALS = "SCAM_SIGNALS"
NOT_A_JOB = "NOT_A_JOB"
SPAM_REPOST = "SPAM_REPOST"
LOW_CONFIDENCE = "LOW_CONFIDENCE"
REJECT_CODES = (
    EXPIRED,
    EMPTY_JD,
    EMPLOYER_MISMATCH,
    AGENCY_POSTING,
    SCAM_SIGNALS,
    NOT_A_JOB,
    SPAM_REPOST,
    LOW_CONFIDENCE,
)

# Độ tin cậy ban đầu theo cách bóc tin.
_BASE_CONFIDENCE = {
    ExtractionMethod.ATS_API: 0.95,
    ExtractionMethod.JSONLD: 0.92,
    ExtractionMethod.PARSER: 0.9,
    ExtractionMethod.HEURISTIC: 0.85,  # trang có đủ dấu hiệu JD, không có LLM
    ExtractionMethod.LLM: 0.75,  # LLM bóc: cần xác nhận lại
}
SUSPECT_CONFIDENCE = 0.6
EMPLOYER_MATCH = 88.0  # điểm rapidfuzz coi là cùng tên
EMPLOYER_DIFFERENT = 60.0  # dưới mức này là tên khác hẳn
SPAM_JD_THRESHOLD = 0.9


@dataclass(slots=True)
class CompanyContext:
    """Những gì đã biết về doanh nghiệp để đối chiếu tin."""

    company_id: int
    name: str
    names: list[str] = field(default_factory=list)  # tên input/pháp lý/thương hiệu/alias
    profile_names: list[str] = field(default_factory=list)  # tên profile đã map trên các trang
    domains: list[str] = field(default_factory=list)
    tax_code: str | None = None
    kyc_status: str | None = None

    def __post_init__(self) -> None:
        self.domains = list(dict.fromkeys(registrable_domain(d) for d in self.domains if d))

    def name_keys(self) -> list[str]:
        return [k for k in {employer_key(n) for n in (self.name, *self.names)} if k]

    def profile_keys(self) -> list[str]:
        return [k for k in {employer_key(n) for n in self.profile_names} if k]

    def describe(self) -> str:
        lines = [f"Tên: {self.name}"]
        others = [n for n in dict.fromkeys(self.names) if n != self.name]
        if others:
            lines.append("Tên khác: " + "; ".join(others[:8]))
        if self.tax_code:
            lines.append(f"MST: {self.tax_code}")
        if self.domains:
            lines.append("Website: " + ", ".join(self.domains[:5]))
        if self.profile_names:
            lines.append("Tên trên trang tuyển dụng: " + "; ".join(self.profile_names[:5]))
        return "\n".join(lines)


@dataclass(slots=True)
class Verdict:
    accepted: bool
    confidence: float
    reasons: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)  # điểm cần LLM xem lại
    llm: bool = False

    @property
    def code(self) -> str | None:
        """Mã loại chính (None nếu nhận)."""
        if self.accepted:
            return None
        return next(
            (r.split(":")[0] for r in self.reasons if r.split(":")[0] in REJECT_CODES), None
        )

    def reject(self, code: str, detail: str | None = None) -> None:
        self.accepted = False
        self.reasons.append(f"{code}: {detail}" if detail else code)


# --- nhà tuyển dụng -------------------------------------------------------------------------------

_GENERIC_NAME_TOKENS = frozenset(
    {"viet", "nam", "vietnam", "vn", "group", "holdings", "holding", "international", "global"}
)


def employer_key(name: str | None) -> str:
    """Tên nhà tuyển dụng để so: `normalize_company_name` bỏ "Việt Nam", "Group"..."""
    if not name:
        return ""
    tokens = normalize_company_name(name).split()
    kept = [t for t in tokens if t not in _GENERIC_NAME_TOKENS]
    return " ".join(kept or tokens)


def employer_score(name: str | None, keys: Sequence[str]) -> float:
    """Độ giống (0..100) giữa tên trên tin và tên doanh nghiệp đã biết."""
    key = employer_key(name)
    if not key or not keys:
        return 0.0
    compact = key.replace(" ", "")
    best = 0.0
    for other in keys:
        best = max(
            best,
            float(fuzz.token_set_ratio(key, other)),
            float(fuzz.ratio(compact, other.replace(" ", ""))),
        )
        # "fpt" ⊂ "fpt software": tên ngắn nằm trọn trong tên dài.
        if min(len(compact), len(other)) >= 3 and (
            other.replace(" ", "") in compact or compact in other.replace(" ", "")
        ):
            best = max(best, 90.0)
    return best


# Nơi nộp hồ sơ hợp lệ ngoài domain doanh nghiệp: ATS, biểu mẫu.
_APPLY_HOSTS = frozenset(
    {
        "greenhouse.io",
        "lever.co",
        "ashbyhq.com",
        "workable.com",
        "myworkdayjobs.com",
        "smartrecruiters.com",
        "bamboohr.com",
        "recruitee.com",
        "teamtailor.com",
        "base.vn",
        "google.com",
        "forms.gle",
        "office.com",
        "microsoft.com",
        "typeform.com",
        "jotform.com",
    }
)


def _apply_domain_ok(job: NormalizedJob, ctx: CompanyContext) -> bool:
    if not job.apply_url:
        return True
    domain = registrable_domain(host_of(job.apply_url))
    if not domain or domain == registrable_domain(host_of(job.url)):
        return True
    if domain in ctx.domains or domain in _APPLY_HOSTS:
        return True
    label = fold(domain.split(".")[0]).replace(" ", "")
    return any(
        len(label) >= 3 and (label in key.replace(" ", "") or key.replace(" ", "") in label)
        for key in ctx.name_keys()
    )


# --- mẫu câu --------------------------------------------------------------------------------------

_AGENCY = re.compile(
    r"tuyen (dung )?(cho|giup) (doi tac|khach hang)"
    r"|(khach hang|doi tac) cua chung toi (la|dang can|dang tim|can tuyen|hien dang)"
    r"|\bour client (is|a|an)\b|on behalf of (our|a) client|\bclient of ours\b"
    r"|(cong ty|ten cong ty|thong tin cong ty) (se )?(duoc )?bao mat"
    r"|bao mat (ten|thong tin) (cong ty|khach hang)"
    r"|confidential (company|client)|(company|client) (name )?(is|will be) confidential"
    r"|\bheadhunt"
)
_AGENCY_NAME = re.compile(
    r"\b(bao mat|confidential|an danh|headhunt\w*|nhan luc|recruitment|staffing|manpower|"
    r"executive search|cung ung lao dong|tu van tuyen dung|talent solutions)\b"
)
# Tín hiệu lừa đảo chắc chắn (loại) và nhẹ (nhờ LLM xem).
_SCAM_STRONG = re.compile(
    r"(nop|dong|thu|dat coc) (phi|tien) (ho so|dao tao|dong phuc|dang ky|giu cho|tuyen dung|"
    r"gioi thieu|xu ly ho so|kham suc khoe|the nhan vien)"
    r"|(ung vien|nguoi lao dong|ban) (phai|can|se) (dat coc|nop tien|dong tien|chuyen khoan)"
    r"|dat coc (dong phuc|ho so|tien|truoc)"
    r"|viec nhe luong cao"
    r"|(cong tac vien|ctv) online|kiem tien online|viec lam online tai nha|nhap lieu tai nha"
    r"|\bda cap\b"
)
_SCAM_WEAK = re.compile(
    r"(khong can|khong yeu cau) (kinh nghiem|bang cap).{0,60}(thu nhap|luong) "
    r"(khung|cuc khung|cao ngat|len den \d+ (trieu|tr))"
    r"|\btelegram\b"
    r"|\d+ ?(k|trieu|tr) ?(moi )?ngay\b.{0,40}\b(ctv|cong tac vien)\b"
)
_NEGATION = re.compile(r"(khong|mien|free|no)\s+(phai\s+)?(la\s+)?(thu\s+)?$")
_EXPIRED_TEXT = re.compile(
    r"tin (tuyen dung )?(nay )?da het han|vi tri (nay )?da (duoc tuyen|dong|du nguoi)"
    r"|no longer accepting applications|this job (posting )?(has )?expired"
    r"|position has been filled|job is no longer available|job has been closed"
)
_GENERIC_TITLES = frozenset(
    {
        "tuyen dung",
        "co hoi nghe nghiep",
        "careers",
        "career",
        "jobs",
        "job openings",
        "open positions",
        "openings",
        "viec lam",
        "tin tuyen dung",
        "join us",
        "join our team",
        "work with us",
        "vi tri tuyen dung",
        "cac vi tri tuyen dung",
        "thong bao tuyen dung",
        "danh sach viec lam",
        "trang chu",
        "home",
        "gioi thieu",
        "about us",
        "lien he",
        "contact",
        "contact us",
        "tin tuc",
        "news",
        "blog",
        "van hoa",
        "van hoa doanh nghiep",
        "life at",
        "doi song",
        "chinh sach",
        "privacy policy",
    }
)
_MULTI_POSITION_TITLE = re.compile(
    r"^(thong bao )?tuyen dung (thang \d+|nhieu vi tri|\d+ vi tri|cac vi tri|dot \d+|nam \d{4})"
)
_NEWS_SEGMENTS = frozenset(
    {
        "blog",
        "blogs",
        "news",
        "tin-tuc",
        "bai-viet",
        "su-kien",
        "event",
        "events",
        "press",
        "article",
    }
)
_TITLE_FILLERS = re.compile(r"\b(tai|at|cua|of|with|voi|cung)\b")


def _fold_head(text: str | None, limit: int = 4000) -> str:
    return fold(text[:limit]) if text else ""


def _strong_scam(text: str) -> str | None:
    for m in _SCAM_STRONG.finditer(text):
        if _NEGATION.search(text[max(0, m.start() - 20) : m.start()]):
            continue
        return m.group(0)
    return None


def _news_url(job: NormalizedJob) -> bool:
    return any(seg.lower() in _NEWS_SEGMENTS for seg in path_segments(job.url))


def _not_a_job(job: NormalizedJob, ctx: CompanyContext) -> str | None:
    title = fold(job.title)
    if len(job.title) > 200 or len(title) < 2:
        return "tiêu đề không phải tên vị trí"
    stripped = title
    for key in ctx.name_keys():
        stripped = re.sub(rf"\b{re.escape(key)}\b", " ", stripped)
    stripped = " ".join(_TITLE_FILLERS.sub(" ", stripped).split())
    if title in _GENERIC_TITLES or stripped in _GENERIC_TITLES or not stripped:
        return "trang danh mục/giới thiệu"
    if _MULTI_POSITION_TITLE.match(title):
        return "thông báo tuyển nhiều vị trí"
    return None


# --- rule -----------------------------------------------------------------------------------------


def check_rules(
    job: NormalizedJob,
    ctx: CompanyContext,
    *,
    now: datetime,
    min_jd_chars: int = 200,
    board_max_age: timedelta = timedelta(days=180),
) -> Verdict:
    """Áp các rule cho một tin: trả quyết định loại, hoặc nhận tạm kèm độ tin cậy + điểm cần xem."""
    verdict = Verdict(accepted=True, confidence=_BASE_CONFIDENCE.get(job.raw.method, 0.7))
    verdict.reasons.append("SOURCE_BOARD" if job.board else f"SOURCE_{job.source.upper()}")
    head = " ".join(
        [fold(job.title), _fold_head(job.raw.company_name), _fold_head(job.description_md)]
    )
    full = " ".join([head, _fold_head(job.requirements_md), _fold_head(job.benefits_md)])

    # EXPIRED
    if job.expires_at is not None and job.expires_at < now:
        verdict.reject(EXPIRED, f"hết hạn {job.expires_at.date().isoformat()}")
    elif _EXPIRED_TEXT.search(head[:1500]):
        verdict.reject(EXPIRED, "trang ghi đã hết hạn")
    elif (
        job.board
        and job.expires_at is None
        and job.posted_at is not None
        and job.posted_at < now - board_max_age
    ):
        verdict.reject(EXPIRED, f"đăng từ {job.posted_at.date().isoformat()}, không có hạn nộp")

    # NOT_A_JOB. Doanh nghiệp nhỏ hay đăng tin tuyển dụng ở mục tin tức: URL dạng tin tức chỉ là
    # điểm cần LLM xem.
    if (why := _not_a_job(job, ctx)) is not None:
        verdict.reject(NOT_A_JOB, why)
    elif _news_url(job):
        verdict.concerns.append("URL dạng tin tức/blog")

    # EMPTY_JD
    if len(job.jd_text) < min_jd_chars:
        verdict.reject(EMPTY_JD, f"{len(job.jd_text)} ký tự")

    # EMPLOYER_MISMATCH
    employer = job.raw.company_name
    known = ctx.name_keys() + ctx.profile_keys()
    score = employer_score(employer, known) if employer else None
    if score is not None and score >= EMPLOYER_MATCH:
        verdict.reasons.append("EMPLOYER_MATCH")
    elif score is not None and job.board and score < EMPLOYER_DIFFERENT:
        verdict.reject(EMPLOYER_MISMATCH, f"nhà tuyển dụng trên tin: {employer}")
    elif score is not None:
        verdict.concerns.append(f"tên nhà tuyển dụng trên tin khác: {employer}")
    elif job.board:
        verdict.concerns.append("tin không ghi nhà tuyển dụng")
    if not _apply_domain_ok(job, ctx):
        verdict.reject(EMPLOYER_MISMATCH, f"link ứng tuyển trỏ về {host_of(job.apply_url or '')}")

    # AGENCY_POSTING. Nhà tuyển dụng trên tin là đơn vị cung ứng nhân sự (kể cả khi profile của
    # nó đã được map vào doanh nghiệp) — trừ khi chính doanh nghiệp là đơn vị đó.
    own_agency = any(_AGENCY_NAME.search(k) for k in ctx.name_keys())
    if (m := _AGENCY.search(full)) is not None:
        verdict.reject(AGENCY_POSTING, m.group(0))
    elif re.search(r"\bconfidential\b", fold(job.title)):
        verdict.reject(AGENCY_POSTING, "tiêu đề ghi confidential")
    elif (
        employer
        and not own_agency
        and employer_score(employer, ctx.name_keys()) < EMPLOYER_MATCH
        and (m := _AGENCY_NAME.search(employer_key(employer))) is not None
    ):
        verdict.reject(AGENCY_POSTING, f"nhà tuyển dụng là đơn vị tuyển dụng hộ: {employer}")

    # SCAM_SIGNALS
    if (hit := _strong_scam(full)) is not None:
        verdict.reject(SCAM_SIGNALS, hit)
    elif (m := _SCAM_WEAK.search(full)) is not None or re.search(r"\bda cap\b", full):
        verdict.concerns.append(f"dấu hiệu lừa đảo: {m.group(0) if m else 'đa cấp'}")

    if job.raw.method is ExtractionMethod.LLM:
        verdict.concerns.append("tin do LLM bóc từ trang không có dữ liệu cấu trúc")
    if verdict.concerns:
        verdict.confidence = min(verdict.confidence, SUSPECT_CONFIDENCE)
    return verdict


def mark_reposts(
    jobs: Sequence[NormalizedJob], verdicts: Sequence[Verdict], sigs: Signatures
) -> int:
    """`SPAM_REPOST`: cùng nguồn đăng lại nhiều lần một tin (tiêu đề, nơi làm, JD gần như y hệt).
    Giữ bản mới nhất (JD dài hơn khi bằng nhau), loại các bản còn lại. Trả số tin bị loại."""
    idx = [i for i, v in enumerate(verdicts) if v.accepted]

    def linked(a: int, b: int) -> bool:
        x, y = jobs[idx[a]], jobs[idx[b]]
        return (
            x.source == y.source
            and x.places == y.places  # giữ tên tỉnh cũ: "[Hải Dương]" ≠ "[Hải Phòng]"
            and title_similarity(x.title_normalized, y.title_normalized) >= 90
            and jd_similarity(sigs(x), sigs(y)) >= SPAM_JD_THRESHOLD
        )

    def newest(i: int) -> tuple[float, int]:
        posted = jobs[i].posted_at
        return (posted.timestamp() if posted else 0.0, len(jobs[i].jd_text))

    rejected = 0
    for group in connected_groups(len(idx), linked):
        if len(group) < 2:
            continue
        members = sorted((idx[g] for g in group), key=newest, reverse=True)
        keep = jobs[members[0]]
        for i in members[1:]:
            verdicts[i].reject(SPAM_REPOST, f"đăng lại của {keep.url}")
            rejected += 1
    return rejected


# --- LLM ------------------------------------------------------------------------------------------


def _job_brief(job: NormalizedJob) -> str:
    raw = job.raw
    lines = [
        f"Tiêu đề: {job.title}",
        f"Nhà tuyển dụng ghi trên tin: {raw.company_name or '(không ghi)'}",
        f"URL: {job.url}",
    ]
    if job.apply_url and job.apply_url != job.url:
        lines.append(f"Link ứng tuyển: {job.apply_url}")
    if job.locations:
        lines.append(
            "Địa điểm: "
            + "; ".join(loc.address_raw or loc.province or "" for loc in job.locations[:3])
        )
    if job.salary and job.salary.raw:
        lines.append(f"Lương: {job.salary.raw}")
    return "\n".join(lines)


VERIFY_CACHE_SECONDS = 7 * 86_400


def _cache_id(job: NormalizedJob, ctx: CompanyContext) -> str:
    return f"{ctx.company_id}:{job.content_hash}"


async def _llm_check(
    job: NormalizedJob,
    verdict: Verdict,
    ctx: CompanyContext,
    llm: LLMClient,
    min_confidence: float,
    kv: KeyValueStore | None,
) -> None:
    result = await cached_structured(
        llm,
        kv,
        _cache_id(job, ctx),
        job_verify.PROMPT,
        job_verify.JobVerification,
        ttl=VERIFY_CACHE_SECONDS,
        company=ctx.describe(),
        source=job.source,
        job=_job_brief(job),
        concerns="; ".join(verdict.concerns) or "không có",
        jd=job.jd_text,
    )
    if result.value is None:
        verdict.reject(LOW_CONFIDENCE, f"LLM không trả lời ({result.error})")
        return
    _apply_llm(verdict, result.value, min_confidence)


def _apply_llm(verdict: Verdict, value: job_verify.JobVerification, min_confidence: float) -> None:
    verdict.llm = True
    verdict.confidence = value.confidence
    why = "; ".join(r.strip()[:200] for r in value.reasons[:3] if r.strip())
    if not value.is_job_posting:
        verdict.reject(NOT_A_JOB, f"LLM: {why}")
    elif not value.belongs_to_company:
        verdict.reject(EMPLOYER_MISMATCH, f"LLM: {why}")
    elif value.is_agency:
        verdict.reject(AGENCY_POSTING, f"LLM: {why}")
    elif value.scam_risk >= 0.5:
        verdict.reject(SCAM_SIGNALS, f"LLM: {why}")
    elif value.confidence < min_confidence:
        verdict.reject(LOW_CONFIDENCE, f"LLM {value.confidence:.2f}: {why}")
    else:
        verdict.reasons.append(f"LLM_CONFIRMED: {why}" if why else "LLM_CONFIRMED")


async def verify_jobs(
    jobs: Sequence[NormalizedJob],
    ctx: CompanyContext,
    llm: LLMClient,
    *,
    now: datetime,
    until: float,
    min_confidence: float = 0.8,
    min_jd_chars: int = 200,
    board_max_age: timedelta = timedelta(days=180),
    max_llm: int = 40,
    sigs: Signatures | None = None,
    kv: KeyValueStore | None = None,
) -> list[Verdict]:
    """Xác minh mọi tin của một doanh nghiệp. LLM chỉ chấm tin còn nghi ngờ (tối đa `max_llm`
    lời gọi, dừng ở `until` theo `time.monotonic()`); tin nghi ngờ không được chấm thì bị loại.
    Câu trả lời của LLM được cache theo doanh nghiệp + nội dung tin (`kv`, 7 ngày)."""
    sigs = sigs or Signatures()
    verdicts = [
        check_rules(job, ctx, now=now, min_jd_chars=min_jd_chars, board_max_age=board_max_age)
        for job in jobs
    ]
    mark_reposts(jobs, verdicts, sigs)

    suspects = [i for i, v in enumerate(verdicts) if v.accepted and v.confidence < min_confidence]
    remaining = list(suspects)
    if suspects and kv is not None:
        keys = [cache_key(job_verify.PROMPT, _cache_id(jobs[i], ctx)) for i in suspects]
        remaining = []
        for i, raw in zip(suspects, await kv.mget(keys), strict=True):
            try:
                value = job_verify.JobVerification.model_validate_json(raw) if raw else None
            except ValueError:
                value = None
            if value is None:
                remaining.append(i)
            else:
                _apply_llm(verdicts[i], value, min_confidence)
    asked = remaining[:max_llm] if llm.enabled else []
    budget = until - time.monotonic()
    if asked and budget > 1:
        tasks = {
            asyncio.ensure_future(_llm_check(jobs[i], verdicts[i], ctx, llm, min_confidence, kv)): i
            for i in asked
        }
        _, pending = await asyncio.wait(tasks, timeout=budget)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    for i in suspects:
        v = verdicts[i]
        if v.accepted and not v.llm:
            why = "LLM chưa cấu hình" if not llm.enabled else "không kịp / vượt giới hạn LLM"
            v.reject(LOW_CONFIDENCE, f"{'; '.join(v.concerns)} ({why})")

    for job, v in zip(jobs, verdicts, strict=True):
        if not v.accepted:
            log.info("verify.reject", url=job.url, title=job.title, reasons=v.reasons)
    codes = Counter(v.code for v in verdicts if not v.accepted)
    log.info(
        "verify.done",
        jobs=len(jobs),
        accepted=sum(v.accepted for v in verdicts),
        llm=sum(v.llm for v in verdicts),
        rejected=dict(codes),
    )
    return verdicts
