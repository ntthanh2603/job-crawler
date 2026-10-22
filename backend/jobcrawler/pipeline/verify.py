"""Xác minh tin đã chuẩn hoá: rule loại trước (mỗi rule một mã lý do), tin còn nghi ngờ thì LLM
chấm; mọi quyết định nhận/loại đều kèm lý do.

Mã loại: `EXPIRED`, `EMPTY_JD`, `EMPLOYER_MISMATCH`, `AGENCY_POSTING`, `SCAM_SIGNALS`,
`NOT_A_JOB`, `SPAM_REPOST`, và `LOW_CONFIDENCE` (còn nghi ngờ mà LLM không xác nhận được).

Độ tin cậy ban đầu theo nguồn và cách bóc (ATS API > JSON-LD > parser > heuristic > LLM). Tin có
điểm cần xem lại (tên nhà tuyển dụng khác, dấu hiệu lừa đảo nhẹ, bóc bằng LLM...) bị hạ dưới
ngưỡng và phải được LLM xác nhận (`confidence ≥ VERIFY_MIN_CONFIDENCE`, mặc định 0.8). Không có
LLM thì loại: ưu tiên chính xác hơn số lượng. Có LLM nhưng hết giờ / vượt giới hạn / LLM lỗi thì
hoãn (`DEFERRED`): không nhận, không loại, lượt sau chấm tiếp (câu trả lời đã có được cache).
"""

from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID

from rapidfuzz import fuzz

from jobcrawler.common.logging import get_logger
from jobcrawler.common.schemas import ExtractionMethod
from jobcrawler.common.text import fold, normalize_company_name
from jobcrawler.common.urls import host_of, path_segments, registrable_domain
from jobcrawler.pipeline import provinces
from jobcrawler.pipeline.dedup import (
    Signatures,
    band_pairs,
    bands_exact,
    connected_groups,
    is_repost,
    jd_similarity,
    jd_threshold_for,
    title_similarity,
)
from jobcrawler.pipeline.normalize import NormalizedJob
from jobcrawler.prompts import job_verify
from jobcrawler.services.llm import LLMClient, cache_key, cached_structured
from jobcrawler.services.net.kv import KeyValueStore

log = get_logger(__name__)

EXPIRED = "EXPIRED"
EMPTY_JD = "EMPTY_JD"
EMPLOYER_MISMATCH = "EMPLOYER_MISMATCH"
AGENCY_POSTING = "AGENCY_POSTING"
SCAM_SIGNALS = "SCAM_SIGNALS"
NOT_A_JOB = "NOT_A_JOB"
SPAM_REPOST = "SPAM_REPOST"
LOW_CONFIDENCE = "LOW_CONFIDENCE"
# Không phải mã loại: tin nghi ngờ chưa được LLM chấm trong lượt này.
DEFERRED = "DEFERRED"
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

    company_id: UUID
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
    deferred: bool = False  # chưa chấm xong: không nhận, không loại

    @property
    def code(self) -> str | None:
        """Mã loại chính (None nếu nhận, `DEFERRED` nếu hoãn)."""
        if self.accepted:
            return None
        if self.deferred:
            return DEFERRED
        return next(
            (r.split(":")[0] for r in self.reasons if r.split(":")[0] in REJECT_CODES), None
        )

    def reject(self, code: str, detail: str | None = None) -> None:
        self.accepted = False
        self.reasons.append(f"{code}: {detail}" if detail else code)

    def defer(self, detail: str) -> None:
        self.accepted = False
        self.deferred = True
        self.reasons.append(f"{DEFERRED}: {detail}")


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
    r"|(khach hang|doi tac) cua chung toi (dang can|dang tim|can tuyen|hien dang)"
    # "Khách hàng của chúng tôi là một tập đoàn FDI" (không phải "là các doanh nghiệp lớn").
    r"|(khach hang|doi tac) cua chung toi la (mot )?(cong ty|tap doan|doanh nghiep|ngan hang"
    r"|nha may|to chuc)"
    r"|\bour client (is )?(looking|seeking|hiring|searching|recruiting)\b"
    r"|on behalf of (our|a) client|\bclient of ours\b"
    r"|(ten|thong tin) (ve )?(cong ty|khach hang|doi tac)( tuyen dung)? (se )?(duoc )?bao mat"
    r"|bao mat ten (cong ty|khach hang|doi tac)"
    r"|confidential (company|client)|(company|client) (name )?(is|will be) confidential"
)
# Câu của nhà tuyển dụng hộ hay của chính doanh nghiệp (công ty gia công phần mềm cũng nói "our
# client is a US bank"; JD của chuyên viên tuyển dụng nhắc "headhunt"): để LLM xem.
_AGENCY_WEAK = re.compile(r"\bour client is (a|an|one)\b|\bheadhunt")
# "Đảm bảo thông tin khách hàng được bảo mật": nhiệm vụ của vị trí, không phải giấu tên công ty.
_DUTY = re.compile(
    r"(dam bao|giu|tuan thu|cam ket|chiu trach nhiem|quy dinh|quan ly|nham|de)( \w+){0,3} ?$"
)
_AGENCY_NAME = re.compile(
    r"\b(bao mat|confidential|an danh|headhunt\w*|nhan luc|recruitment|staffing|manpower|"
    r"executive search|cung ung lao dong|tu van tuyen dung|talent solutions)\b"
)
# Tín hiệu lừa đảo chắc chắn (loại) và nhẹ (nhờ LLM xem).
# Văn bản đã `fold` (bỏ dấu): "đa cấp" không so ở đây vì trùng "đã cấp" (đã cấp phát).
_SCAM_STRONG = re.compile(
    r"(nop|dong|dat coc) phi (ho so|dao tao|dong phuc|dang ky|giu cho|tuyen dung|"
    r"gioi thieu|xu ly ho so|kham suc khoe|the nhan vien)"
    # Thu tiền/phí của ứng viên ("thu tiền đăng ký khoá học" là việc của thu ngân, không tính).
    r"|thu (phi|tien) (ho so|dong phuc|giu cho|tuyen dung|xu ly ho so|the nhan vien)"
    r"|(ung vien|nguoi lao dong|ban) (phai|can|se) (dat coc|nop tien|dong tien|nop phi|dong phi"
    r"|chuyen khoan)"
    r"|dat coc (dong phuc|ho so|tien|truoc)"
    r"|viec nhe luong cao"
    r"|kiem tien online"
)
_SCAM_WEAK = re.compile(
    r"(khong can|khong yeu cau) (kinh nghiem|bang cap).{0,60}(thu nhap|luong) "
    r"(khung|cuc khung|cao ngat|len den \d+ (trieu|tr))"
    r"|\btelegram\b"
    r"|\d+ ?(k|trieu|tr) ?(moi )?ngay\b.{0,40}\b(ctv|cong tac vien)\b"
    r"|(nop|dong) tien (ho so|dao tao|dong phuc|dang ky|giu cho|tuyen dung|xu ly ho so"
    r"|kham suc khoe|the nhan vien)"
    # Cộng tác viên làm online có thật (Ahamove "CTV Kinh Doanh"): để LLM xem, không loại ngay.
    r"|(cong tac vien|ctv) online|viec lam online tai nha|nhap lieu tai nha"
)
# So trên chữ còn dấu (NFC, viết thường).
_DA_CAP = re.compile(r"(?<!\w)đa cấp(?!\w)")
_NEGATION = re.compile(r"(khong|mien|free|no)\s+(phai\s+)?(la\s+)?(thu\s+)?$")
# Người trả là công ty: "Công ty đóng phí đào tạo cho nhân viên".
_COMPANY_PAYS = re.compile(r"(cong ty|chung toi|doanh nghiep)( se| ho tro| chi tra)? ?$")
_EXPIRED_TEXT = re.compile(
    r"tin (tuyen dung )?(nay )?da het han|vi tri (nay )?da (duoc tuyen|dong|du nguoi)"
    r"|no longer accepting applications|this job (posting )?(has )?expired"
    r"|position has been filled|job is no longer available|job has been closed"
)
# Thời điểm tuyển ghi trong tiêu đề / URL: "Tuyển dụng 11/ 2019", "tháng 3-2024",
# "tuyen-dung-11-2019".
# "11-2019" trong tiêu đề / URL; dấu "/" ngay sau năm vẫn được (cuối đường dẫn "…-11-2019/").
_DATED = re.compile(r"(?<![\d/.])(0?[1-9]|1[0-2])\s*[/-]\s*(20\d{2})(?![\d.]|/\d)")
_DATED_MAX_AGE = timedelta(days=183)


def _stale_mention(job: NormalizedJob, now: datetime) -> str | None:
    """Tháng/năm cũ hơn nửa năm trong tiêu đề hoặc URL (bài tuyển dụng cũ còn trên site)."""
    for text in (job.title, job.url):
        for m in _DATED.finditer(text):
            month, year = int(m[1]), int(m[2])
            mentioned = datetime(year, month, 1, tzinfo=UTC)
            if mentioned < now - _DATED_MAX_AGE:
                return f"{month:02d}/{year}"
    return None


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
        "tuyen dung nhan su",
        "thong tin tuyen dung",
        "co hoi viec lam",
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
# Phần còn lại của tiêu đề "Tuyển dụng 05 vị trí Kỹ sư cầu đường" có tên vị trí thật không.
_ANNOUNCEMENT_WORDS = frozenset(
    {"thang", "nam", "dot", "cac", "vi", "tri", "nhieu", "moi", "nhat", "tai", "o", "nhan", "su"}
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
# Trang danh sách / điều hướng của site tuyển dụng: "11 việc làm đang chờ bạn", "Tất cả việc làm".
_LISTING_TITLE = re.compile(
    r"^(?:\d+ )?(?:co hoi nghe nghiep|co hoi|viec lam|vi tri|cong viec|jobs?|positions?|openings?)"
    r"(?: (?:dang cho ban|dang tuyen|dang mo|hien co|moi nhat|available|open))?$"
    r"|^(?:xem )?tat ca(?: (?:viec lam|vi tri|co hoi|cong viec))?$"
    r"|^(?:view )?all(?: open)? (?:jobs|roles|positions|openings)$"
    # Trang nhóm nghề của site tuyển dụng tập đoàn: "Corporate Social Responsibility Jobs",
    # "Sales jobs in Hanoi".
    r"|^[a-z0-9 &/-]{3,60} jobs(?: (?:in|at) [a-z0-9 ,-]+)?$"
)
# Tin của doanh nghiệp khác đăng trên trang của doanh nghiệp (trang hợp tác doanh nghiệp của
# trường đại học, trang tuyển dụng tập đoàn): "MITEK VIỆT NAM TUYỂN DỤNG NHÂN VIÊN",
# `/tuyen-dung/cong-ty-tnhh-mitek-viet-nam-tuyen-dung-...`.
_POSTER_IN_TITLE = re.compile(r"^(?P<name>[a-z0-9 &.-]{3,60}?) (?:tuyen dung|can tuyen|tuyen)\b")
_POSTER_IN_SLUG = re.compile(
    r"^(?P<name>cong ty (?:tnhh |co phan |cp )?(?:mtv )?[a-z0-9 ]{3,60}?) (?:tuyen dung|tuyen)\b"
)
_COMPANY_WORDS = re.compile(r"\b(cong ty|tnhh|co phan|jsc|ltd|corporation|tap doan|viet nam)\b")
_LEGAL_WORDS = re.compile(r"\b(cong ty|tnhh|co phan|jsc|ltd|corporation|tap doan)\b")
# Trang SEO / kết quả tìm / khẩu hiệu của site việc làm: "tìm việc làm ở Thanh Hóa",
# "Tìm thấy 4 công việc Thiết kế", "… 39192 công việc đang chờ".
_LISTING_SEO = re.compile(
    r"^(?:tim )?(?:viec lam|cong viec)(?: moi| nhanh)? (?:o|tai) "
    r"|^tim thay \d+ (?:cong viec|viec lam|vi tri|ket qua)\b"
    r"|\b\d{3,} (?:cong viec|viec lam|vi tri|jobs)\b"
)
# Tiêu đề chỉ là tên tỉnh/thành (trang lọc việc theo nơi làm): "Tỉnh Vĩnh Long".
_PLACE_PREFIX = re.compile(r"^(?:tinh|thanh pho|tp|khu vuc|mien) ")


def _fold_head(text: str | None, limit: int = 4000) -> str:
    return fold(text[:limit]) if text else ""


def _strong_scam(text: str, original: str) -> str | None:
    """`text`: văn bản đã fold; `original`: chữ còn dấu (để nhận "đa cấp")."""
    for m in _SCAM_STRONG.finditer(text):
        before = text[max(0, m.start() - 25) : m.start()]
        if _NEGATION.search(before) or _COMPANY_PAYS.search(before):
            continue
        return m.group(0)
    for m in _DA_CAP.finditer(original):
        if _NEGATION.search(fold(original[max(0, m.start() - 20) : m.start()]) + " "):
            continue
        return "đa cấp"
    return None


def _agency(text: str) -> str | None:
    for m in _AGENCY.finditer(text):
        if m.group(0).startswith(("ten ", "thong tin ")) and _DUTY.search(
            text[max(0, m.start() - 40) : m.start()]
        ):
            continue
        return m.group(0)
    return None


def _original(*parts: str | None, limit: int = 4000) -> str:
    return " ".join(unicodedata.normalize("NFC", p[:limit]).lower() for p in parts if p)


def _other_poster(job: NormalizedJob, known: Sequence[str]) -> str | None:
    """Tên doanh nghiệp khác ghi là bên tuyển ở đầu tiêu đề / slug URL ("<Công ty X> tuyển dụng
    ..."), khi tên đó trông như tên doanh nghiệp và khác hẳn doanh nghiệp đang xét."""
    segs = path_segments(job.url)
    slug = fold(segs[-1].replace("-", " ")) if segs else ""
    found: list[str] = []
    for text, pattern in ((fold(job.title), _POSTER_IN_TITLE), (slug, _POSTER_IN_SLUG)):
        m = pattern.match(text)
        if m is None or not _COMPANY_WORDS.search(m["name"]):
            continue
        if employer_score(m["name"], known) < EMPLOYER_DIFFERENT:
            found.append(m["name"])
    # Tên có loại hình pháp lý ("cong ty tnhh mitek viet nam") là bằng chứng mạnh hơn.
    return next((n for n in found if _LEGAL_WORDS.search(n)), found[0] if found else None)


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
    if m := _MULTI_POSITION_TITLE.match(title):
        rest = [w for w in title[m.end() :].split() if not w.isdigit()]
        # Có tên vị trí và JD đủ dài: một tin thật ("Tuyển dụng 05 vị trí Kỹ sư cầu đường").
        if not any(w not in _ANNOUNCEMENT_WORDS for w in rest) or len(job.jd_text) < 400:
            return "thông báo tuyển nhiều vị trí"
    if _LISTING_TITLE.match(title) or _LISTING_SEO.search(title):
        return "trang danh sách việc làm"
    if provinces.province_pattern().fullmatch(_PLACE_PREFIX.sub("", title)):
        return "trang lọc theo tỉnh/thành"
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
        # Trang careers cũng hay để lại bài tuyển dụng cũ (2020–2023) trong mục tin tức.
        (job.board or job.source == "careers")
        and job.expires_at is None
        and job.posted_at is not None
        and job.posted_at < now - board_max_age
    ):
        verdict.reject(EXPIRED, f"đăng từ {job.posted_at.date().isoformat()}, không có hạn nộp")
    elif job.expires_at is None and (stale := _stale_mention(job, now)) is not None:
        verdict.reject(EXPIRED, f"tiêu đề/URL ghi thời điểm tuyển cũ ({stale})")

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
    if (poster := _other_poster(job, known)) is not None:
        if _LEGAL_WORDS.search(poster):
            verdict.reject(EMPLOYER_MISMATCH, f"tin của doanh nghiệp khác: {poster}")
        else:
            verdict.concerns.append(f"có thể là tin của doanh nghiệp khác: {poster}")

    # AGENCY_POSTING. Nhà tuyển dụng trên tin là đơn vị cung ứng nhân sự (kể cả khi profile của
    # nó đã được map vào doanh nghiệp) — trừ khi chính doanh nghiệp là đơn vị đó.
    own_agency = any(_AGENCY_NAME.search(k) for k in ctx.name_keys())
    if (hit := _agency(full)) is not None:
        verdict.reject(AGENCY_POSTING, hit)
    elif re.search(r"\bconfidential\b", fold(job.title)):
        verdict.reject(AGENCY_POSTING, "tiêu đề ghi confidential")
    elif (
        employer
        and not own_agency
        and employer_score(employer, ctx.name_keys()) < EMPLOYER_MATCH
        and (m := _AGENCY_NAME.search(employer_key(employer))) is not None
    ):
        verdict.reject(AGENCY_POSTING, f"nhà tuyển dụng là đơn vị tuyển dụng hộ: {employer}")
    elif (m := _AGENCY_WEAK.search(full)) is not None:
        verdict.concerns.append(f"có thể là tin tuyển hộ: {m.group(0)}")

    # SCAM_SIGNALS
    original = _original(
        job.title, job.raw.company_name, job.description_md, job.requirements_md, job.benefits_md
    )
    if (hit := _strong_scam(full, original)) is not None:
        verdict.reject(SCAM_SIGNALS, hit)
    elif (m := _SCAM_WEAK.search(full)) is not None or _DA_CAP.search(original):
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
        if x.source != y.source or x.places != y.places:  # "[Hải Dương]" ≠ "[Hải Phòng]"
            return False
        if not x.places:
            # Không biết nơi làm: tiêu đề chuẩn hoá đã bỏ phần trong ngoặc ("[VII.2026_Ba Đình] CV
            # khách hàng" của từng chi nhánh Vietcombank) nên so tiêu đề gốc, đòi gần như y hệt.
            return is_repost(x.title, y.title, jd_similarity(sigs(x), sigs(y)))
        threshold = jd_threshold_for(x.title_normalized, y.title_normalized, SPAM_JD_THRESHOLD)
        return (
            title_similarity(x.title_normalized, y.title_normalized) >= 90
            and jd_similarity(sigs(x), sigs(y)) >= threshold
        )

    def newest(i: int) -> tuple[float, int]:
        posted = jobs[i].posted_at
        return (posted.timestamp() if posted else 0.0, len(jobs[i].jd_text))

    # Đăng lại đòi JD ≥ 0,9 và cùng nguồn: chỉ so các cặp cùng nguồn trùng một băng LSH.
    assert bands_exact(SPAM_JD_THRESHOLD)
    kept = [jobs[i] for i in idx]
    pairs = sorted(band_pairs([sigs(x) for x in kept], [x.source for x in kept]))
    rejected = 0
    for group in connected_groups(len(idx), linked, pairs=pairs):
        if len(group) < 2:
            continue
        members = sorted((idx[g] for g in group), key=newest, reverse=True)
        keep = jobs[members[0]]
        for i in members[1:]:
            if jobs[i].external_id == keep.external_id:
                continue  # chính tin đó, bóc hai lần: không phải đăng lại (gom cụm sẽ gộp)
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
        verdict.defer(f"LLM không trả lời ({result.error})")
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
    lời gọi, dừng ở `until` theo `time.monotonic()`); tin nghi ngờ không được chấm thì bị loại
    khi không có LLM, hoãn (`Verdict.deferred`) khi có LLM mà không kịp. Câu trả lời của LLM được
    cache theo doanh nghiệp + nội dung tin (`kv`, 7 ngày)."""
    sigs = sigs or Signatures()

    def rules() -> list[Verdict]:
        checked = [
            check_rules(job, ctx, now=now, min_jd_chars=min_jd_chars, board_max_age=board_max_age)
            for job in jobs
        ]
        mark_reposts(jobs, checked, sigs)
        return checked

    # Luật + so đăng lại là việc CPU: chạy trong thread để event loop của runner không đứng.
    verdicts = await asyncio.to_thread(rules)

    suspects = [i for i, v in enumerate(verdicts) if v.accepted and v.confidence < min_confidence]
    remaining = list(suspects)
    if suspects and kv is not None:
        keys = [cache_key(job_verify.PROMPT, _cache_id(jobs[i], ctx), llm.model) for i in suspects]
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
            concerns = "; ".join(v.concerns)
            if llm.enabled:
                v.defer(f"{concerns} (không kịp / vượt giới hạn LLM)")
            else:
                v.reject(LOW_CONFIDENCE, f"{concerns} (LLM chưa cấu hình)")

    for job, v in zip(jobs, verdicts, strict=True):
        if not v.accepted and not v.deferred:
            log.info("verify.reject", url=job.url, title=job.title, reasons=v.reasons)
    codes = Counter(v.code for v in verdicts if not v.accepted)
    log.info(
        "verify.done",
        jobs=len(jobs),
        accepted=sum(v.accepted for v in verdicts),
        deferred=sum(v.deferred for v in verdicts),
        llm=sum(v.llm for v in verdicts),
        rejected=dict(codes),
    )
    return verdicts
