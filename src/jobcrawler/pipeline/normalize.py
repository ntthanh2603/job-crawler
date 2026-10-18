"""Chuẩn hoá một `RawJob` (giá trị thô của nguồn) thành `NormalizedJob` (các trường của `JobOut`).

- Văn bản: HTML → markdown, ftfy, bỏ boilerplate (nút chia sẻ, "ứng tuyển ngay", "việc làm
  liên quan"...), emoji trang trí, khoảng trắng thừa; ẩn SĐT, email cá nhân, tên người liên hệ.
- Lương: "10 - 15 triệu", "Tới 30 triệu", "Từ 1,000 USD", "$1.5k-2k", "Thỏa thuận"... → min/max,
  tiền tệ, kỳ trả.
- Địa điểm: 34 tỉnh/thành sau sáp nhập 2025 (`pipeline.provinces`).
- Cấp bậc, loại hình, hình thức làm việc: rule; còn `unknown` thì `classify_unclear` hỏi LLM
  (chỉ cho job đã nhận và đã gộp, để không tốn LLM cho tin bị loại).
- Ngày: về UTC; hiểu "2 ngày trước", "Hạn nộp: 30/11/2025", "Còn 15 ngày".

Mọi hàm ở đây là thuần (không I/O) trừ `classify_unclear`.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from urllib.parse import urljoin

import ftfy
from markdown_it import MarkdownIt

from jobcrawler.llm import LLMClient, cache_key, cached_structured
from jobcrawler.logging import get_logger
from jobcrawler.net.kv import KeyValueStore
from jobcrawler.pages import html_to_markdown
from jobcrawler.pipeline import provinces
from jobcrawler.prompts import job_fields
from jobcrawler.schemas import (
    EmploymentType,
    ExperienceYears,
    JobLevel,
    Location,
    RawJob,
    Salary,
    SalaryPeriod,
    WorkMode,
)
from jobcrawler.sources.base import VN_TZ, parse_datetime
from jobcrawler.text import fold, strip_accents

log = get_logger(__name__)

CONTACT_MASK = "[liên hệ đã ẩn]"


@dataclass(slots=True)
class NormalizedJob:
    raw: RawJob
    board: bool  # tin trên trang tuyển dụng (không phải careers/ATS của doanh nghiệp)
    title: str
    title_normalized: str
    description_md: str
    requirements_md: str | None = None
    benefits_md: str | None = None
    level: JobLevel = JobLevel.UNKNOWN
    employment_type: EmploymentType = EmploymentType.UNKNOWN
    work_mode: WorkMode = WorkMode.UNKNOWN
    locations: list[Location] = field(default_factory=list)
    salary: Salary | None = None
    experience_years: ExperienceYears | None = None
    skills: list[str] = field(default_factory=list)
    posted_at: datetime | None = None
    expires_at: datetime | None = None
    apply_url: str | None = None
    # Nơi làm việc chi tiết hơn tỉnh mới: giữ tên tỉnh cũ ("Hải Dương" ≠ "Hải Phòng"), lấy từ
    # địa chỉ và tiêu đề. Dùng khi so hai tin của cùng một nguồn.
    places: frozenset[str] = frozenset()

    @property
    def source(self) -> str:
        return self.raw.source

    @property
    def external_id(self) -> str:
        return self.raw.external_id

    @property
    def url(self) -> str:
        return self.raw.url

    @property
    def content_hash(self) -> str:
        """Hash nội dung thô của tin (`job_postings.content_hash`, khoá cache LLM)."""
        return hashlib.sha256(self.raw.model_dump_json().encode()).hexdigest()[:32]

    @property
    def provinces(self) -> set[str]:
        return {loc.province for loc in self.locations if loc.province}

    @property
    def jd_text(self) -> str:
        """Mô tả + yêu cầu (phần dùng để đo độ dài JD và so trùng)."""
        return "\n\n".join(p for p in (self.description_md, self.requirements_md) if p)

    @property
    def unclear_fields(self) -> list[str]:
        return [
            name
            for name, value in (
                ("level", self.level),
                ("employment_type", self.employment_type),
                ("work_mode", self.work_mode),
            )
            if value.value == "unknown"
        ]


# --- văn bản -------------------------------------------------------------------------------------

# Emoji và ký hiệu trang trí (giữ chữ, số, dấu câu thường).
_DECOR = re.compile("[\U0001f000-\U0001faff☀-➿⬀-⯿←-⇿■-◿✀-➿️‍⃣​⁠]")
# Ký hiệu đầu dòng dùng như gạch đầu dòng → "- " ("**Đậm**" không phải gạch đầu dòng).
_BULLET_SYMBOLS = "•●○◦▪▫■□◆◇❖➤►▶▸✓✔✅☑→⇒·‣⁃\U0001f000-\U0001faff☀-➿"
_BULLET = re.compile(rf"^\s*(?:[-+*](?=\s)|o(?=\s)|[{_BULLET_SYMBOLS}])[\s{_BULLET_SYMBOLS}️]*")
_TABLE_RULE = re.compile(r"^\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?$")
_JSONLD_REPR = re.compile(r"^\{'@type':.*\}$")  # dict JSON-LD bị chuyển thành chuỗi
_MD_MARKS = re.compile(r"[#*_>`|\[\]]+")
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_SPACES = re.compile(r"[ \t 　]+")
_SHARE_LINK = re.compile(
    r"(facebook\.com/sharer|twitter\.com/intent|x\.com/intent|linkedin\.com/share|"
    r"zalo\.me/share|plus\.google\.com/share|mailto:\?)",
    re.IGNORECASE,
)
_BOILERPLATE_LINES = frozenset(
    {
        "ung tuyen",
        "ung tuyen ngay",
        "nop don",
        "nop don ngay",
        "nop ho so",
        "nop ho so ngay",
        "apply",
        "apply now",
        "apply for this job",
        "apply for this position",
        "chia se",
        "chia se tin",
        "chia se tin tuyen dung",
        "share",
        "share this job",
        "luu",
        "luu tin",
        "luu viec lam",
        "bao cao",
        "bao cao tin tuyen dung",
        "xem them",
        "xem tat ca",
        "xem chi tiet",
        "thu gon",
        "quay lai",
        "back",
        "back to jobs",
        "dang nhap",
        "dang ky",
        "copy link",
        "sao chep link",
        "sao chep lien ket",
        "facebook",
        "linkedin",
        "twitter",
        "zalo",
        "print",
        "in",
    }
)
# Từ tiêu đề mục này trở đi là phần khác của trang (tin liên quan...): cắt bỏ.
_CUTOFF_HEADINGS = (
    "viec lam lien quan",
    "viec lam tuong tu",
    "tin tuyen dung lien quan",
    "co the ban quan tam",
    "cac vi tri khac",
    "vi tri tuyen dung khac",
    "viec lam khac",
    "similar jobs",
    "related jobs",
    "other jobs",
    "more jobs",
    "jobs you may like",
    "you may also like",
)


def _plain_line(line: str) -> str:
    return fold(_MD_MARKS.sub(" ", _MD_LINK.sub(r"\1", line)))


# Gạch đầu dòng viết liền chữ: "-Khi trở thành..." (không phải "-5%"). "*Tham gia BHXH" do
# `_fix_emphasis` xử lý.
_TIGHT_BULLET = re.compile(r"^\s*[-+](?=[^\W\d_])")


def _clean_line(line: str) -> str:
    tight = _TIGHT_BULLET.match(line)
    if tight is not None:
        line = "- " + line[tight.end() :]
    bullet = _BULLET.match(line)
    if bullet is not None and bullet.end() < len(line):
        line = "- " + line[bullet.end() :]
    line = _DECOR.sub("", line)
    return _SPACES.sub(" ", line).strip()


# Hai đoạn nhấn mạnh liền nhau: "**Tuổi****:**" → "**Tuổi:**", "***a******b***" → "***ab***".
_EMPHASIS_JOIN = re.compile(r"(?<=[^\s*])(\*{2,3})\1(?=[^\s*])")
# Dấu đóng sau dấu câu mà dính chữ thì CommonMark không đóng được: "**Tuổi:**Từ 18" → thêm cách.
_CLOSE_BEFORE_WORD = re.compile(r"(?:^|(?<=\s))(\*{1,3})(?=[^\s*])([^*\n]*?[^\s\w*])\1(?=[^\W_])")
# Dấu mở thừa khoảng trắng ở đầu dòng: "** Thu nhập:**" → "**Thu nhập:**".
_OPEN_SPACE = re.compile(r"^(\*{2,3})\s+([^*\n]*[^\s*])\1(?=\s|$)")
# Chú thích "(*)", "(**)" (giữ) hoặc một dãy dấu sao nhấn mạnh (bỏ khi dòng vỡ).
_STAR_RUN = re.compile(r"\(\*+\)|\*+")
# Dấu sao hiện thành chữ mà vẫn đúng: chú thích "(*)", dính sau chữ/số "5*", "Sun* Inc.", "Pro*C".
_LITERAL_STAR_OK = re.compile(r"\(\*+\)|(?<=[^\W_])\*")
# Ký hiệu đầu dòng của markdown (tiêu đề, danh sách): không xét khi tìm dấu sao vỡ.
_LINE_MARK = re.compile(r"^\s*(?:#{1,6}|[-+*]|\d{1,3}[.)])\s+")
# Cùng luật CommonMark với lúc hiển thị (web/markdown.py).
_INLINE_MD = MarkdownIt("commonmark")


def _literal_stars(line: str) -> list[str]:
    """Phần dấu sao còn hiện thành chữ trong từng đoạn chữ khi hiển thị dòng (đoạn đầu rỗng nếu
    dòng mở đầu bằng in đậm/nghiêng), đã bỏ các dấu sao hợp lệ."""
    out: list[str] = []
    for token in _INLINE_MD.parseInline(line):
        for child in token.children or ():
            text = _LITERAL_STAR_OK.sub("", child.content) if child.type == "text" else ""
            out.append(text)
    return out


def _broken_emphasis(line: str) -> bool:
    """Hiển thị dòng này còn dấu sao thừa (không thành in đậm/nghiêng, không phải chữ thật)."""
    mark = _LINE_MARK.match(line)
    return any("*" in t for t in _literal_stars(line[mark.end() :] if mark else line))


def _fix_emphasis(line: str) -> str:
    """Sửa dấu sao vỡ: nối/thêm cách cho in đậm viết dính; còn vỡ (trang dùng "*" làm ký hiệu
    trang trí bên trong thẻ in đậm, VNPay: "*** Số lượng cần tuyển: 06**") thì bỏ hết dấu nhấn
    mạnh (giữ chú thích "(*)"); mở đầu bằng một dãy lẻ dấu sao thừa là một mục danh sách."""
    if "*" not in line:
        return line
    mark = _LINE_MARK.match(line)
    prefix, body = (mark.group(0), line[mark.end() :]) if mark else ("", line)
    body = _EMPHASIS_JOIN.sub("", body)
    body = _CLOSE_BEFORE_WORD.sub(r"\1\2\1 ", body)
    body = _OPEN_SPACE.sub(r"\1\2\1", body)
    texts = _literal_stars(body)
    if not any("*" in t for t in texts):
        return prefix + body
    lead = len(body) - len(body.lstrip("*"))
    # "*** Số lượng…", "*Tham gia BHXH" (không phải "** Join…" hay "*Hãy kể* ***…").
    item = bool(texts) and texts[0].startswith("*") and lead % 2 == 1
    body = _STAR_RUN.sub(lambda m: m.group(0) if m.group(0)[0] == "(" else "", body)
    body = _SPACES.sub(" ", body).strip()
    return f"- {body}" if item and not prefix else prefix + body


# Link markdown (chữ có thể chứa một cặp ngoặc, href có thể kèm tiêu đề):
# "[Tìm hiểu thêm](/careers/why-bosch/ "…")", "[[HN] Data Analyst](/tuyen-dung/hn-data-analyst/)".
_LINK = re.compile(r"(?<!!)\[((?:[^\[\]]|\[[^\[\]]*\])*)\]\(([^()\s]+)(?:\s+\"[^\"]*\")?\)")


def _fix_links(md: str, base_url: str | None) -> str:
    """Link tương đối → tuyệt đối theo URL tin (để trên giao diện không trỏ về chính app);
    không có URL gốc hoặc link "#…"/"javascript:" thì chỉ giữ chữ."""

    def repl(m: re.Match[str]) -> str:
        text, href = m.group(1), m.group(2)
        if re.match(r"(?:https?|mailto|tel):", href, re.IGNORECASE):
            return m.group(0)
        if href.startswith("#") or href.lower().startswith("javascript:") or not base_url:
            return text
        return f"[{text}]({urljoin(base_url, href)})"

    return _LINK.sub(repl, md)


def to_markdown(text: str | None, fmt: str = "html", base_url: str | None = None) -> str:
    """Nội dung thô (HTML/markdown/text) → markdown sạch, chưa ẩn liên hệ. `base_url` (URL tin)
    để đổi link tương đối thành tuyệt đối."""
    if not text or not text.strip():
        return ""
    # Giữ ngoặc kép cong “…”: đúng chính tả, và uncurl khiến làm sạch lần hai cho kết quả khác
    # (lần đầu ngoặc cong còn nằm trong entity HTML).
    text = ftfy.fix_text(text, uncurl_quotes=False)
    md = html_to_markdown(text) if fmt == "html" else text
    # Dấu viết bằng entity ("o&#769;") chỉ được giải mã ở bước trên, sau ftfy: dựng lại NFC.
    md = unicodedata.normalize("NFC", md)
    md = _fix_links(md, base_url)
    out: list[str] = []
    for raw_line in md.replace("\r\n", "\n").split("\n"):
        cleaned = _clean_line(raw_line)
        line = _fix_emphasis(cleaned)
        if line != cleaned:  # bỏ dấu sao có thể lộ ký hiệu đầu dòng ("*+ Kỹ năng…*" → "+ Kỹ năng…")
            line = _clean_line(line)
        if not line:
            if out and out[-1]:
                out.append("")
            continue
        if _JSONLD_REPR.match(line) or _SHARE_LINK.search(line):
            continue
        if _TABLE_RULE.match(line):
            out.append(line)
            continue
        plain = _plain_line(line)
        if len(line) <= 60 and any(
            plain == h or plain.startswith(h + " ") for h in _CUTOFF_HEADINGS
        ):
            break
        if plain in _BOILERPLATE_LINES or (not plain and not line.startswith("-")):
            continue
        if line in ("-", "- "):
            continue
        out.append(line)
    # Dòng trống / tiêu đề mục không có nội dung ở cuối (vd. "## Thông tin ứng tuyển").
    while out and (not out[-1] or out[-1].startswith("#")):
        out.pop()
    return "\n".join(out).strip()


# --- tách mục JD ----------------------------------------------------------------------------------

_SECTION_KEYS: dict[str, tuple[str, ...]] = {
    "requirements": (
        "yeu cau",
        "tieu chuan",
        "dieu kien ung tuyen",
        "ky nang yeu cau",
        "requirements",
        "requirement",
        "job requirements",
        "qualifications",
        "skills and experience",
        "your skills and experience",
        "what you need",
        "what we are looking for",
        "what we re looking for",
        "who you are",
        "your profile",
        "about you",
        "must have",
    ),
    "benefits": (
        "quyen loi",
        "phuc loi",
        "che do phuc loi",
        "che do dai ngo",
        "dai ngo",
        "thu nhap va phuc loi",
        "luong thuong va phuc loi",
        "benefits",
        "perks",
        "what we offer",
        "why you ll love working here",
        "why join us",
    ),
    "description": (
        "mo ta cong viec",
        "mo ta",
        "chi tiet cong viec",
        "nhiem vu",
        "trach nhiem",
        "job description",
        "description",
        "responsibilities",
        "what you will do",
        "what you ll do",
        "about the role",
        "the role",
        "your role",
        # Mục khác của JD: để trong phần mô tả.
        "dia diem lam viec",
        "thoi gian lam viec",
        "cach thuc ung tuyen",
        "ho so bao gom",
        "thong tin lien he",
        "how to apply",
        "about us",
        "ve chung toi",
        "gioi thieu cong ty",
    ),
}


def _section_of(line: str) -> str | None:
    if len(line) > 80:
        return None
    plain = _plain_line(line.rstrip(":： "))
    if not plain or len(plain.split()) > 7:
        return None
    for section, keys in _SECTION_KEYS.items():
        if any(plain == k or plain.startswith(k + " ") for k in keys):
            return section
    return None


def split_sections(md: str) -> tuple[str, str | None, str | None]:
    """Một khối JD có tiêu đề mục ("Mô tả công việc", "Yêu cầu", "Quyền lợi") → (mô tả, yêu cầu,
    quyền lợi). Không nhận ra mục yêu cầu/quyền lợi thì giữ nguyên ở mô tả."""
    parts: dict[str, list[str]] = {"description": [], "requirements": [], "benefits": []}
    current = "description"
    for line in md.split("\n"):
        section = _section_of(line) if line.strip() else None
        if section is not None:
            current = section
            if section != "description":
                continue  # tiêu đề mục đã thành tên trường
        parts[current].append(line)
    req = _tidy(parts["requirements"])
    ben = _tidy(parts["benefits"])
    if not req and not ben:
        return md, None, None
    return _tidy(parts["description"]), req or None, ben or None


def _tidy(lines: list[str]) -> str:
    """Gộp dòng trống liên tiếp, bỏ dòng trống / tiêu đề mục không có nội dung ở cuối (sau khi
    tách mục, như cuối `to_markdown`)."""
    out: list[str] = []
    for line in lines:
        if not line.strip() and (not out or not out[-1].strip()):
            continue
        out.append(line)
    while out and (not out[-1].strip() or out[-1].lstrip().startswith("#")):
        out.pop()
    return "\n".join(out).strip()


# --- ẩn liên hệ cá nhân ---------------------------------------------------------------------------

_PHONE = re.compile(
    r"(?<![\w.,/+-])"
    r"(?:\(?\+84\)?[\s.\-]?\(?0?|\(?0)\d{1,3}\)?(?:[\s.\-]?\d){6,9}"
    r"(?![\w/])"
    r"|(?<![\w.,/])1[89]00[\s.\-]?\d{3,4}(?:[\s.\-]?\d{2,4})?(?![\w/])"
)
_TAX_LABEL = re.compile(r"(mst|ma so thue|ma so doanh nghiep|tax code|tax id|msdn|so dkkd)\W*$")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", re.UNICODE)
_FREE_MAIL = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "yahoo.com.vn",
        "hotmail.com",
        "outlook.com",
        "outlook.com.vn",
        "live.com",
        "icloud.com",
        "me.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
        "mail.ru",
        "yandex.com",
        "zoho.com",
        "gmx.com",
        "qq.com",
        "163.com",
        "naver.com",
    }
)
# Hộp thư ứng tuyển chung của công ty (được giữ).
_GENERIC_MAILBOX = frozenset(
    {
        "hr",
        "hrm",
        "hrd",
        "hcns",
        "nhansu",
        "tuyendung",
        "td",
        "recruit",
        "recruitment",
        "recruiting",
        "recruiter",
        "career",
        "careers",
        "job",
        "jobs",
        "talent",
        "talents",
        "hiring",
        "cv",
        "apply",
        "people",
        "info",
        "contact",
        "lienhe",
        "support",
        "hotro",
        "admin",
        "office",
        "vn",
        "hn",
        "hcm",
        "dn",
    }
)
# Nhãn đứng trước tên người liên hệ ("Liên hệ:", "Tên / Chức danh:", "Họ tên:", "Người phụ trách
# tuyển dụng:"); nhãn dài đặt trước để khớp hết.
_CONTACT_LABEL = (
    r"(?:t(?:ê|e)n\s*/\s*ch(?:ứ|u)c\s+danh"
    r"|t(?:ê|e)n\s+ng(?:ư|u)(?:ờ|o)i\s+li(?:ê|e)n\s+h(?:ệ|e)"
    r"|h(?:ọ|o)\s+(?:v(?:à|a)\s+)?t(?:ê|e)n"
    r"|ng(?:ư|u)(?:ờ|o)i\s+ph(?:ụ|u)\s+tr(?:á|a)ch(?:\s+tuy(?:ể|e)n\s+d(?:ụ|u)ng)?"
    r"|ng(?:ư|u)(?:ờ|o)i\s+li(?:ê|e)n\s+h(?:ệ|e)|li(?:ê|e)n\s+h(?:ệ|e)|lh"
    r"|contact(?:\s+person)?|recruiter)"
)
_HONORIFIC = r"(?:mr|ms|mrs|miss|anh|ch(?:ị|i)|a\.|c\.|c(?:ô|o)|th(?:ầ|a)y)"
_NAME_WORD = r"[^\W\d_][^\W\d_'’.-]*"
# Khoảng trắng trong một dòng. Tên chỉ được nằm ở dòng sau nhãn khi nhãn có dấu hai chấm
# ("Liên hệ:\nNguyễn Văn An" từ ô bảng); "Thông tin liên hệ\n- Tên / Chức danh: …" không khớp.
_SP = r"[^\S\n]"
# "Liên hệ: Ms. Hương", "Người liên hệ: Nguyễn Văn A", "LH chị Lan"
_LABELED_NAME = re.compile(
    rf"(\b{_CONTACT_LABEL}{_SP}*(?::\s*|[\-–]{_SP}*|))"
    rf"((?:{_HONORIFIC}\.?{_SP}+)?{_NAME_WORD}(?:{_SP}+{_NAME_WORD}){{0,3}})",
    re.IGNORECASE,
)
# "Ms. Hương", "Mr Nam" ở bất kỳ đâu.
_TITLED_NAME = re.compile(
    rf"\b(?:Mr|Ms|Mrs|Miss)\.?{_SP}+{_NAME_WORD}(?:{_SP}+{_NAME_WORD}){{0,3}}"
)
# Từ đầu không phải tên người ("Liên hệ: Phòng Nhân sự", "Liên hệ Công ty").
_NOT_NAME_START = frozenset(
    {
        "phong",
        "bo",
        "ban",
        "cong",
        "nhan",
        "tuyen",
        "chung",
        "email",
        "so",
        "sdt",
        "hotline",
        "dien",
        "qua",
        "theo",
        "tai",
        "dia",
        "website",
        "zalo",
        "hr",
        "van",
        "trung",
        "khach",
        "ngay",
        "truc",
        "the",
        "our",
        "us",
        "with",
        "via",
    }
)


def _honorific(word: str) -> bool:
    return re.fullmatch(rf"{_HONORIFIC}\.?", word, re.IGNORECASE) is not None


def _person_name(text: str) -> str | None:
    """Phần đầu của `text` là tên người (các từ viết hoa, có hoặc không có danh xưng)."""
    words = text.split()
    has_title = bool(words) and _honorific(words[0])
    kept: list[str] = words[:1] if has_title else []
    for word in words[len(kept) :]:
        if not word[:1].isupper():
            break
        kept.append(word)
    names = kept[1:] if has_title else kept
    if not names or fold(names[0]) in _NOT_NAME_START:
        return None
    if not has_title and len(names) < 2:  # một từ viết hoa không có danh xưng: chưa chắc
        return None
    return " ".join(kept)


def _mask_email(m: re.Match[str]) -> str:
    email = m.group(0)
    local, _, domain = email.lower().rpartition("@")
    if domain in _FREE_MAIL:
        return CONTACT_MASK
    tokens = [t for t in re.split(r"[._+\-\d]+", local) if t]
    if tokens and all(t in _GENERIC_MAILBOX for t in tokens):
        return email
    return CONTACT_MASK


# Email Cloudflare giấu ("[email protected]", link /cdn-cgi/l/email-protection): không giải mã,
# chỉ thay bằng nhãn ẩn.
_CF_EMAIL = re.compile(
    r"\[?\[email[\s\xa0]protected\]\]?(?:\([^)\s]*\))?|\[[^\[\]]*\]\([^)\s]*/cdn-cgi/l/email-protection[^)\s]*\)",
    re.IGNORECASE,
)


def mask_contacts(text: str) -> str:
    """Ẩn số điện thoại, email cá nhân, tên người liên hệ; giữ email ứng tuyển chung của công ty
    (hr@, tuyendung@...) và URL."""
    if not text:
        return text

    def phone(m: re.Match[str]) -> str:
        before = fold(text[max(0, m.start() - 25) : m.start()])
        return m.group(0) if _TAX_LABEL.search(before) else CONTACT_MASK

    text = _CF_EMAIL.sub(CONTACT_MASK, text)
    text = _EMAIL.sub(_mask_email, text)
    text = _PHONE.sub(phone, text)

    # Không dùng `sub`: khớp bị loại ("Liên hệ: Tên / Chức danh: Vũ Tuấn") không được nuốt phần
    # sau nhãn, vì phần đó có thể chứa nhãn thật — tìm tiếp từ đầu phần tên.
    out: list[str] = []
    pos = 0
    while m := _LABELED_NAME.search(text, pos):
        name = _person_name(m.group(2))
        if name is None:
            out.append(text[pos : m.start(2)])
            pos = m.start(2)
            continue
        out.append(text[pos : m.start(2)] + CONTACT_MASK)
        pos = m.start(2) + len(name)
    text = "".join(out) + text[pos:]

    def titled(m: re.Match[str]) -> str:
        name = _person_name(m.group(0))
        return CONTACT_MASK + m.group(0)[len(name) :] if name else m.group(0)

    text = _TITLED_NAME.sub(titled, text)
    mask = re.escape(CONTACT_MASK)
    # Link tới liên hệ đã ẩn ("[[liên hệ đã ẩn]](mailto:[liên hệ đã ẩn])") → chỉ còn nhãn ẩn.
    text = re.sub(
        rf"\[(?:[^\[\]]|{mask})*\]\([^()\s]*{mask}[^()\s]*(?:\s+\"[^\"]*\")?\)", CONTACT_MASK, text
    )
    return re.sub(rf"{mask}(?:[\s,/;:–-]*{mask})+", CONTACT_MASK, text)


def clean_text_field(text: str | None, fmt: str = "html", base_url: str | None = None) -> str:
    masked = mask_contacts(to_markdown(text, fmt, base_url))
    if "*" not in masked or CONTACT_MASK not in masked:
        return masked
    # Nhãn ẩn có ngoặc vuông nên có thể làm vỡ in đậm/nghiêng bao quanh: "*0222…*" → "*[liên hệ
    # đã ẩn]*" — sửa lại các dòng đó.
    return "\n".join(
        _fix_emphasis(line) if CONTACT_MASK in line else line for line in masked.split("\n")
    )


# --- tiêu đề --------------------------------------------------------------------------------------

_TITLE_SPLIT = re.compile(r"\s+[-–—|]\s+|\s*\|\s*|\s+[-–—]\s*$|^\s*[-–—]\s+")
_SALARY_HINT = re.compile(
    r"\b(luong|thu nhap|salary|income|upto|up to|len den|toi da)\b"
    r"|\d[\d.,]*\s*(k|m|tr|trieu|cu|usd|vnd|d)\b|\$\s*\d|\d\s*\$"
)
_URGENT = re.compile(r"^(tuyen gap|can gap|gap|urgent|hot|hot job|tuyen dung gap|new|moi)$")
_EXPERIENCE_HINT = re.compile(
    r"^(khong (yeu cau|can) kinh nghiem|chua (co|can) kinh nghiem|no experience( required)?|"
    r"\d+\+? (nam|years?)( kinh nghiem| exp(erience)?)?|tren \d+ nam( kinh nghiem)?)$"
)
_CODE = re.compile(r"^(?:id|ma|code|req|job id|ref)?\s*#?\s*[a-z]*\d{3,}[a-z\d]*$")
_PLACE_MARKERS = re.compile(
    r"\b(tai|lam viec tai|khu vuc|tp|thanh pho|tinh|quan|huyen|phuong|xa|cn|chi nhanh)\b"
)
_JOINERS = re.compile(r"\b(o|va|and|hoac|or|\d+)\b")
_TRAILING_PLACE = re.compile(r"\s+(?:tai|o|lam viec tai|khu vuc)\s+(.+)$")


def _only_places(folded: str, raw: str = "") -> bool:
    """Đoạn tiêu đề chỉ nêu nơi làm việc: "HN/HCM", "Đồng Nai", "Phường 1 Bảo Lộc, Lâm Đồng"."""
    pattern = provinces.province_pattern()
    if not pattern.search(folded):
        return False
    rest = _JOINERS.sub(" ", _PLACE_MARKERS.sub(" ", pattern.sub(" ", folded))).split()
    if not rest:
        return True
    # Còn vài từ (tên phường/huyện) thì phải có dấu hiệu là địa chỉ: dấu phẩy hoặc "phường"...
    return len(rest) <= 2 and ("," in raw or _PLACE_MARKERS.search(folded) is not None)


def _noise_segment(segment: str) -> bool:
    f = fold(segment)
    if not f:
        return True
    return bool(
        _SALARY_HINT.search(f)
        or _URGENT.match(f)
        or _EXPERIENCE_HINT.match(f)
        or _CODE.match(f)
        or _only_places(f, segment)
    )


def clean_title(title: str) -> str:
    return _SPACES.sub(" ", _DECOR.sub("", ftfy.fix_text(title))).strip(" -–|")


def normalize_title(title: str) -> str:
    """Khoá so khớp của tiêu đề: bỏ dấu, viết thường, bỏ phần lương / địa điểm / "tuyển gấp" /
    mã tin ("Kỹ Sư Hiện Trường (20-30Tr)" → "ky su hien truong")."""
    text = clean_title(title)
    text = re.sub(
        r"[(\[]([^()\[\]]*)[)\]]", lambda m: " " if _noise_segment(m.group(1)) else m.group(0), text
    )
    segments = [s for s in _TITLE_SPLIT.split(text) if s and s.strip()]
    kept = [s for s in segments if not _noise_segment(s)] or segments[:1]
    folded = fold(" ".join(kept))
    tail = _TRAILING_PLACE.search(folded)
    if tail is not None and _only_places(tail.group(1)):
        folded = folded[: tail.start()]
    return folded.strip() or fold(title)


# --- lương ----------------------------------------------------------------------------------------

_NEGOTIABLE = re.compile(
    r"\b(thoa thuan|thuong luong|canh tranh|negotiable|negotiate|competitive|theo nang luc|"
    r"hap dan|tbd)\b"
)
_SAL_NUM = re.compile(r"(\d+(?:[.,]\d+)*)(?!\d)\s*(?:(trieu|tr|ty|cu|nghin|ngan|k|m)(?![a-z]))?")
_MULTIPLIER = {
    "k": 1e3,
    "nghin": 1e3,
    "ngan": 1e3,
    "m": 1e6,
    "tr": 1e6,
    "trieu": 1e6,
    "cu": 1e6,
    "ty": 1e9,
}
_VND_UNITS = frozenset({"tr", "trieu", "cu", "ty", "nghin", "ngan"})
_MAX_ONLY = re.compile(r"\b(up ?to|toi|len (den|toi)|den|max|toi da|duoi|under|below)\b|<")
_MIN_ONLY = re.compile(
    r"\b(tu|from|tren|over|hon|min|toi thieu|above|starting|at least|it nhat)\b|>"
)
_CURRENCIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("USD", re.compile(r"\$|\busd\b|us dollar|\bdo la\b")),
    ("EUR", re.compile(r"€|\beur\b")),
    ("JPY", re.compile(r"¥|\bjpy\b|\byen\b")),
    ("SGD", re.compile(r"\bsgd\b")),
    ("VND", re.compile(r"₫|\bvnd\b|\bdong\b|\d\s*d\b")),
)
# Tháng trước: "20 triệu/tháng, thưởng theo năm" là lương tháng.
_PERIODS: tuple[tuple[SalaryPeriod, re.Pattern[str]], ...] = (
    (SalaryPeriod.MONTH, re.compile(r"\b(thang|th|month|monthly|mo)\b")),
    (SalaryPeriod.HOUR, re.compile(r"\b(gio|hour|hourly|hr|h)\b")),
    (SalaryPeriod.YEAR, re.compile(r"\b(nam|year|yearly|annual|annually|annum|yr|pa)\b")),
)


def _salary_lower(text: str) -> str:
    return strip_accents(text).lower()


def parse_number(text: str) -> float | None:
    """ "1,000" / "1.000.000" / "1.5" / "1,5" / "12,500,000" → số."""
    if not text:
        return None
    seps = [c for c in text if c in ".,"]
    if not seps:
        return float(text)
    last = max(text.rfind("."), text.rfind(","))
    tail = text[last + 1 :]
    if len(set(seps)) == 1 and (len(seps) > 1 or len(tail) == 3):
        return float(text.replace(seps[0], ""))
    if len(tail) == 3 and len(set(seps)) > 1:
        return float(re.sub(r"[.,]", "", text))
    head = re.sub(r"[.,]", "", text[:last])
    return float(f"{head}.{tail}")


def _period(text: str) -> SalaryPeriod | None:
    folded = fold(text)
    for period, pattern in _PERIODS:
        if pattern.search(folded):
            return period
    return None


def _currency(text: str) -> str | None:
    for code, pattern in _CURRENCIES:
        if pattern.search(text):
            return code
    return None


def _norm_currency(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    return _currency(_salary_lower(value)) or (value.upper() if value.isalpha() else None)


@dataclass(slots=True)
class _Amounts:
    low: float | None = None
    high: float | None = None
    currency: str | None = None
    period: SalaryPeriod | None = None


def _parse_amounts(text: str) -> _Amounts:
    s = _salary_lower(text)
    found = [(m, parse_number(m.group(1)), m.group(2)) for m in _SAL_NUM.finditer(s)]
    nums = [(m, v, unit) for m, v, unit in found if v is not None]
    out = _Amounts(currency=_currency(s), period=_period(text))
    if not nums:
        return out
    if len(nums) >= 2 and nums[0][2] is None and nums[1][2] is not None:
        nums[0] = (nums[0][0], nums[0][1], nums[1][2])  # "10 - 15 triệu"
    units = {u for _, _, u in nums[:2] if u}
    values = [v * _MULTIPLIER.get(u or "", 1.0) for _, v, u in nums[:2] if v is not None]
    if out.currency is None and units & _VND_UNITS:
        out.currency = "VND"
    prefix = s[: nums[0][0].start()]
    if len(values) >= 2:
        out.low, out.high = sorted(values[:2])
    elif _MAX_ONLY.search(prefix):
        out.high = values[0]
    elif _MIN_ONLY.search(prefix):
        out.low = values[0]
    else:
        out.low = out.high = values[0]
    top = out.high or out.low or 0
    if not units:
        if out.currency is None and top <= 500:
            out.currency = "VND"  # "10 - 15": quen viết tắt triệu đồng
        if out.currency == "VND" and top < 1000:
            out.low = out.low * 1e6 if out.low is not None else None
            out.high = out.high * 1e6 if out.high is not None else None
        elif out.currency is None and top >= 100_000:
            out.currency = "VND"
    return out


def parse_salary(
    raw: str | None,
    *,
    low: float | None = None,
    high: float | None = None,
    currency: str | None = None,
    period: str | None = None,
) -> Salary | None:
    """Lương từ chuỗi thô (và các trường có cấu trúc nếu nguồn có, ưu tiên hơn)."""
    raw = _SPACES.sub(" ", ftfy.fix_text(raw)).strip() if raw else None
    negotiable = bool(raw and _NEGOTIABLE.search(fold(raw)))
    parsed = _parse_amounts(raw) if raw else _Amounts()
    amounts = _Amounts(
        low=low if low is not None else (None if high is not None else parsed.low),
        high=high if high is not None else (None if low is not None else parsed.high),
        currency=_norm_currency(currency) or parsed.currency,
        period=(_period(period) if period else None) or parsed.period,
    )
    # Nguồn ghi 0 khi không công bố ("0 - 0", "0 - 1.600 USD"): 0 không phải mức lương.
    amounts.low = amounts.low or None
    amounts.high = amounts.high or None
    if amounts.low is None and amounts.high is None:
        if not raw and not negotiable:
            return None
        # Không có số: tiền tệ/kỳ trả mặc định của nguồn không có nghĩa.
        return Salary(negotiable=negotiable, raw=raw)
    if amounts.currency == "VND" and max(amounts.low or 0, amounts.high or 0) < 1000:
        amounts.low = amounts.low * 1e6 if amounts.low is not None else None
        amounts.high = amounts.high * 1e6 if amounts.high is not None else None
    if amounts.low is not None and amounts.high is not None and amounts.low > amounts.high:
        amounts.low, amounts.high = amounts.high, amounts.low
    top = max(amounts.low or 0, amounts.high or 0)
    if amounts.period is None:
        yearly = (amounts.currency == "USD" and top >= 30_000) or (
            amounts.currency == "VND" and top >= 500_000_000
        )
        amounts.period = SalaryPeriod.YEAR if yearly else SalaryPeriod.MONTH
    if raw is None:
        raw = _salary_raw(amounts)
    return Salary(
        min=amounts.low,
        max=amounts.high,
        currency=amounts.currency,
        period=amounts.period,
        negotiable=negotiable,
        raw=raw,
    )


# Nhãn lương trong nội dung tin (đã bỏ dấu): "Mức lương:", "**Thu nhập:**", "##### Mức thu nhập".
_SALARY_LABEL = re.compile(
    r"^[\s#>*_-]*(?:\d+[.)]\s*)?[*_]*"
    r"(?:muc luong|luong|muc thu nhap|thu nhap|salary|compensation)"
    r"(?:\s+(?:co ban|tham khao|du kien|offer|range|thang|gross|net))*"
    r"[*_]*\s*[:\-–]?\s*[*_]*\s*(.*)$"
)
# Giá trị trông như mức lương: khoảng số, số kèm đơn vị tiền, hoặc số lớn ("8.000.000").
_SALARY_VALUE = re.compile(
    r"\d[\d.,]*\s*(?:-|–|~|den|to)\s*\$?\d"
    r"|\d[\d.,]*\s*(?:trieu|tr\b|vnd|d\b|usd|\$|k\b|m\b)"
    r"|\$\s*\d"
    r"|\d{1,3}(?:[.,]\d{3}){2,}"
)

# Giá trị chỉ là "thoả thuận" (không nhận "Lương thưởng hấp dẫn").
_NEGOTIABLE_VALUE = re.compile(r"(thoa thuan|thuong luong|negotiable|canh tranh|competitive)\b")


def salary_in_text(*texts: str | None) -> Salary | None:
    """Mức lương ghi trong nội dung tin (nguồn không có trường lương riêng): dòng có nhãn lương
    và giá trị ngay sau nhãn hoặc ở dòng kế tiếp. Chỉ nhận khi giá trị có số tiền rõ ràng hoặc là
    "thoả thuận" — không nhận "Lương tháng 13", "Lương thưởng hấp dẫn"."""
    for text in texts:
        lines = [line.strip() for line in (text or "").splitlines()]
        for i, line in enumerate(lines):
            plain = strip_accents(line).lower()  # giữ dấu câu (khác `fold`)
            m = _SALARY_LABEL.match(plain)
            if m is None:
                continue
            # Bỏ dấu không đổi độ dài với chữ dựng sẵn (NFC): cắt giá trị gốc theo cùng vị trí.
            value = (line[m.start(1) :] if len(plain) == len(line) else m.group(1)).strip()
            if not value:  # nhãn đứng riêng một dòng (tiêu đề mục): giá trị ở dòng có chữ kế tiếp
                value = next((v for v in lines[i + 1 : i + 3] if v), "")
            value = _SPACES.sub(" ", _MD_MARKS.sub(" ", value)).strip(" -–:.…")
            if not value or len(value) > 80:
                continue
            if _SALARY_VALUE.search(strip_accents(value).lower()):
                salary = parse_salary(value)
                if salary is not None and (salary.min is not None or salary.max is not None):
                    return salary
            elif _NEGOTIABLE_VALUE.match(fold(value)) and len(value) <= 30:
                return Salary(negotiable=True, raw=value)
    return None


def _salary_raw(a: _Amounts) -> str:
    def fmt(v: float | None) -> str:
        return f"{v:,.0f}" if v is not None else ""

    period = f"/{a.period.value}" if a.period else ""
    return f"{fmt(a.low)} - {fmt(a.high)} {a.currency or ''}{period}".strip()


# --- kinh nghiệm ----------------------------------------------------------------------------------

_EXP_NONE = re.compile(
    r"\b(khong yeu cau|khong can|chua co kinh nghiem|chua can kinh nghiem|no requirements?|"
    r"no experience|not required|none)\b"
)
_EXP_RANGE = re.compile(
    r"(?:(?P<prefix>it nhat|toi thieu|tren|tu|hon|at least|minimum|min|over|more than|duoi|"
    r"less than|under|toi da|up to)\s+)?"
    r"(?P<a>\d+(?:[.,]\d+)?)\s*(?:(?P<plus>\+)|(?:-|–|~|den|to)\s*(?P<b>\d+(?:[.,]\d+)?))?\s*"
    r"(?P<unit>nam|years?|yrs?|thang|months?)"
)
_EXP_CONTEXT = re.compile(r"kinh nghiem|experience|\bexp\b")


def parse_experience(raw: str | None, *, require_context: bool = False) -> ExperienceYears | None:
    """ "2 - 3 năm", "Trên 3 năm", "12 months", "Không yêu cầu" → số năm tối thiểu/tối đa.

    `require_context`: chỉ nhận khi câu có chữ "kinh nghiệm"/"experience" (đọc từ JD)."""
    if not raw:
        return None
    text = _SPACES.sub(" ", _salary_lower(raw))  # giữ "-", "+" của khoảng năm
    if not require_context and _EXP_NONE.search(text):
        return ExperienceYears(min=0)
    for m in _EXP_RANGE.finditer(text):
        if require_context and not _EXP_CONTEXT.search(text[max(0, m.start() - 40) : m.end() + 40]):
            continue
        scale = 1 / 12 if m["unit"].startswith(("thang", "month")) else 1.0
        a = round(float(m["a"].replace(",", ".")) * scale, 2)
        b = round(float(m["b"].replace(",", ".")) * scale, 2) if m["b"] else None
        if a > 40 or (b is not None and b > 40):
            continue
        prefix = m["prefix"] or ""
        if b is not None:
            return ExperienceYears(min=min(a, b), max=max(a, b))
        if prefix in ("duoi", "less than", "under", "toi da", "up to"):
            return ExperienceYears(min=0, max=a)
        return ExperienceYears(min=a)
    return None


# --- cấp bậc, loại hình, hình thức ----------------------------------------------------------------


def _has(text: str, *phrases: str) -> bool:
    return any(re.search(rf"(?<![0-9a-z]){re.escape(p)}(?![0-9a-z])", text) for p in phrases)


_LEVEL_RULES: tuple[tuple[JobLevel, tuple[str, ...]], ...] = (
    (
        JobLevel.DIRECTOR,
        (
            "director",
            "giam doc",
            "pho giam doc",
            "tong giam doc",
            "vice president",
            "ceo",
            "cto",
            "cfo",
            "coo",
            "cmo",
            "cio",
            "c level",
            "cap cao hon",
        ),
    ),
    (
        JobLevel.MANAGER,
        (
            "manager",
            "truong phong",
            "pho phong",
            "truong bo phan",
            "truong ban",
            "truong chi nhanh",
            "ke toan truong",
            "head of",
            "head",
            "quan ly",
        ),
    ),
    (
        JobLevel.LEAD,
        (
            "lead",
            "leader",
            "team lead",
            "tech lead",
            "truong nhom",
            "nhom truong",
            "truong ca",
            "to truong",
            "supervisor",
            "giam sat",
            "principal",
        ),
    ),
    (JobLevel.SENIOR, ("senior", "sr", "cvcc", "cvc", "chuyen vien cao cap", "chuyen vien chinh")),
    (JobLevel.MIDDLE, ("middle", "mid level", "mid", "intermediate")),
    (JobLevel.JUNIOR, ("junior", "jr")),
    (
        JobLevel.FRESHER,
        ("fresher", "moi tot nghiep", "moi ra truong", "entry level", "graduate", "trainee"),
    ),
    (
        JobLevel.INTERN,
        ("intern", "internship", "thuc tap", "thuc tap sinh", "sinh vien", "student"),
    ),
)
# Có chữ manager/director nhưng không phải cấp quản lý.
_LEVEL_EXCEPTIONS = re.compile(
    r"\b(tro ly|thu ky|assistant|secretary|relationship manager|account manager|"
    r"product manager|customer success manager|community manager|lead generation|lead gen)\b"
)


_IC_TITLES = ("chuyen vien ", "nhan vien ", "cong tac vien ", "specialist ", "staff ")


def classify_level(raw_level: str | None, title: str, category: str | None = None) -> JobLevel:
    """Cấp bậc từ trường cấp bậc của nguồn, rồi từ tiêu đề."""
    for value in (raw_level, category):
        if not value:
            continue
        text = fold(value)
        for level, phrases in _LEVEL_RULES:
            if _has(text, *phrases):
                return level
    text = fold(title)
    # "Chuyên viên/Nhân viên ... (Partnership Manager)", "Nhân viên giám sát chất lượng": vị trí
    # chuyên môn, không suy ra cấp quản lý từ phần sau của tiêu đề.
    blocked = _LEVEL_EXCEPTIONS.search(text) is not None or text.startswith(_IC_TITLES)
    for level, phrases in _LEVEL_RULES:
        if blocked and level in (JobLevel.DIRECTOR, JobLevel.MANAGER, JobLevel.LEAD):
            continue
        if level is JobLevel.MANAGER:
            # "Quản lý"/"head" ở giữa tiêu đề thường là lĩnh vực ("Chuyên viên quản lý rủi ro").
            if _has(text, *(p for p in phrases if p not in ("quan ly", "head"))) or text.startswith(
                ("quan ly ", "head ")
            ):
                return level
            continue
        if level is JobLevel.LEAD and not _has(text, *(p for p in phrases if p != "giam sat")):
            if text.startswith("giam sat "):
                return level
            continue
        if level is JobLevel.INTERN and not _has(text, *(p for p in phrases if p != "sinh vien")):
            continue
        if _has(text, *phrases):
            return level
    return JobLevel.UNKNOWN


_EMPLOYMENT_RULES: tuple[tuple[EmploymentType, tuple[str, ...]], ...] = (
    (EmploymentType.INTERNSHIP, ("intern", "internship", "thuc tap", "thuc tap sinh")),
    (EmploymentType.PART_TIME, ("part time", "parttime", "ban thoi gian")),
    (
        EmploymentType.FREELANCE,
        ("freelance", "freelancer", "cong tac vien", "ctv", "tu do", "self employed"),
    ),
    (
        EmploymentType.CONTRACT,
        (
            "contract",
            "contractor",
            "temporary",
            "temp",
            "thoi vu",
            "hop dong",
            "fixed term",
            "seasonal",
            "per diem",
        ),
    ),
    (
        EmploymentType.FULL_TIME,
        ("full time", "fulltime", "toan thoi gian", "chinh thuc", "permanent", "regular"),
    ),
)


def classify_employment(raw: str | None, title: str) -> EmploymentType:
    """Loại hình: tiêu đề ghi rõ thực tập / bán thời gian / cộng tác viên thì theo tiêu đề (nhiều
    trang để mặc định FULL_TIME), không thì theo trường của nguồn."""
    title_text = fold(title)
    for kind, phrases in _EMPLOYMENT_RULES[:3]:
        if _has(title_text, *phrases):
            return kind
    if raw:
        # Nguồn ghi nhiều loại ("FULL_TIME, PART_TIME"): lấy loại ghi trước.
        text = fold(raw.replace("_", " "))
        first: tuple[int, EmploymentType] | None = None
        for kind, phrases in _EMPLOYMENT_RULES:
            for phrase in phrases:
                m = re.search(rf"(?<![0-9a-z]){re.escape(phrase)}(?![0-9a-z])", text)
                if m is not None and (first is None or m.start() < first[0]):
                    first = (m.start(), kind)
        if first is not None:
            return first[1]
    if _has(title_text, "thoi vu", "contract", "contractor"):
        return EmploymentType.CONTRACT
    return EmploymentType.UNKNOWN


_WORK_MODE_RULES: tuple[tuple[WorkMode, tuple[str, ...]], ...] = (
    (WorkMode.HYBRID, ("hybrid", "ket hop", "lai")),
    (
        WorkMode.REMOTE,
        (
            "remote",
            "telecommute",
            "tu xa",
            "wfh",
            "work from home",
            "lam viec tai nha",
            "lam tai nha",
            "fully remote",
        ),
    ),
    (WorkMode.ONSITE, ("onsite", "on site", "in office", "at office", "tai van phong", "office")),
)


def classify_work_mode(raw: str | None, title: str, locations: Sequence[str] = ()) -> WorkMode:
    texts = [fold(raw.replace("_", " ")) if raw else "", fold(title)]
    texts += [fold(loc) for loc in locations]
    for i, text in enumerate(texts):
        if not text:
            continue
        for mode, phrases in _WORK_MODE_RULES:
            # "lai" (lai = hybrid) chỉ đọc ở trường của nguồn; "office" ở tiêu đề là chức danh.
            usable = phrases if i == 0 else tuple(p for p in phrases if p not in ("lai", "office"))
            if _has(text, *usable):
                return mode
    return WorkMode.UNKNOWN


# --- ngày -----------------------------------------------------------------------------------------

# "2 ngày trước", "một tuần", "a month ago". Số đứng riêng (không dính chữ trước: "Hà Nam" bỏ
# dấu là "ha nam", không phải "a năm"); "a/an" chỉ đi với đơn vị tiếng Anh, "một" với tiếng Việt.
_RELATIVE = re.compile(
    r"(?<![a-z\d])(?:(?P<n>\d+)\+?\s*(?P<unit>phut|minutes?|mins?|gio|hours?|hrs?|ngay|days?|"
    r"tuan|weeks?|thang|months?|nam|years?)"
    r"|mot\s+(?P<vi>phut|gio|ngay|tuan|thang|nam)"
    r"|an?\s+(?P<en>minutes?|mins?|hours?|hrs?|days?|weeks?|months?|years?))"
    r"\b\s*(?P<dir>truoc|ago|nua|left|remaining|con lai)?"
)
_UNITS = {
    "phut": timedelta(minutes=1),
    "min": timedelta(minutes=1),
    "gio": timedelta(hours=1),
    "hour": timedelta(hours=1),
    "hr": timedelta(hours=1),
    "ngay": timedelta(days=1),
    "day": timedelta(days=1),
    "tuan": timedelta(weeks=1),
    "week": timedelta(weeks=1),
    "thang": timedelta(days=30),
    "month": timedelta(days=30),
    "nam": timedelta(days=365),
    "year": timedelta(days=365),
}
_DMY = re.compile(r"(?<!\d)(\d{1,2})\s*[/.\-]\s*(\d{1,2})\s*[/.\-]\s*(\d{4})(?!\d)")
_YMD = re.compile(r"(?<!\d)(\d{4})\s*[/.\-]\s*(\d{1,2})\s*[/.\-]\s*(\d{1,2})(?!\d)")
_VI_DATE = re.compile(r"(?<!\d)(\d{1,2})\s+thang\s+(\d{1,2})\s*,?\s*(?:nam\s+)?(\d{4})")
_MONTHS = {
    m: i
    for i, names in enumerate(
        (
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ),
        start=1,
    )
    for m in names
}
_EN_DATE = re.compile(
    r"(?:(?P<d1>\d{1,2})\s+(?P<m1>[a-z]{3,9})\.?,?\s+(?P<y1>\d{4}))"
    r"|(?:(?P<m2>[a-z]{3,9})\.?\s+(?P<d2>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<y2>\d{4}))"
)


def _unit_delta(unit: str) -> timedelta:
    for key, delta in _UNITS.items():
        if unit.startswith(key):
            return delta
    return timedelta(0)


def _day(d: date, *, end_of_day: bool) -> datetime:
    moment = dtime(23, 59, 59) if end_of_day else dtime(0, 0)
    return datetime.combine(d, moment, VN_TZ).astimezone(UTC)


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _ymd_swapped(m: re.Match[str]) -> tuple[int, int, int]:
    """Ngày dạng tháng/ngày/năm khi ngày/tháng/năm không hợp lệ ("12/31/2026")."""
    return int(m[3]), int(m[1]), int(m[2])


def parse_date(raw: str | None, *, now: datetime, end_of_day: bool = False) -> datetime | None:
    """Ngày thô → UTC. Hiểu ISO/RFC (qua `parse_datetime`), "30/11/2025" (ngày trước tháng),
    "30 tháng 11 năm 2025", "Nov 30, 2025", "2 ngày trước", "hôm qua", "Còn 15 ngày"."""
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    if (iso := parse_datetime(text, end_of_day=end_of_day)) is not None:
        return iso
    folded = fold(text)
    # Khoảng ngày ("01/10/2026 - 31/10/2026"): hạn nộp là ngày muộn nhất, ngày đăng là sớm nhất.
    days = [
        day
        for m in _DMY.finditer(text)
        if (day := _safe_date(int(m[3]), int(m[2]), int(m[1])) or _safe_date(*_ymd_swapped(m)))
    ]
    if days:
        return _day(max(days) if end_of_day else min(days), end_of_day=end_of_day)
    if m := _YMD.search(text):
        day = _safe_date(int(m[1]), int(m[2]), int(m[3]))
        if day is not None:
            return _day(day, end_of_day=end_of_day)
    if m := _VI_DATE.search(folded):
        day = _safe_date(int(m[3]), int(m[2]), int(m[1]))
        if day is not None:
            return _day(day, end_of_day=end_of_day)
    if m := _EN_DATE.search(folded):
        month = _MONTHS.get(m["m1"] or m["m2"] or "")
        if month:
            day = _safe_date(int(m["y1"] or m["y2"]), month, int(m["d1"] or m["d2"]))
            if day is not None:
                return _day(day, end_of_day=end_of_day)
    if _has(folded, "hom nay", "today", "vua xong", "just now", "moi dang", "just posted"):
        return now
    if _has(folded, "hom qua", "yesterday"):
        return now - timedelta(days=1)
    if m := _RELATIVE.search(folded):
        n = int(m["n"]) if m["n"] else 1
        delta = _unit_delta(m["unit"] or m["vi"] or m["en"]) * n
        direction = m["dir"] or ""
        if direction in ("truoc", "ago"):
            return now - delta
        if direction or _has(folded, "con", "con lai"):
            return now + delta
        # Không ghi chiều: hạn nộp tính tới, ngày đăng tính lùi.
        return now + delta if end_of_day else now - delta
    return None


# --- tổng hợp -------------------------------------------------------------------------------------

_MAX_SKILLS = 30


def clean_skills(skills: Iterable[str], title: str) -> list[str]:
    title_key = fold(title)
    out: dict[str, str] = {}
    for skill in skills:
        text = _SPACES.sub(" ", ftfy.fix_text(str(skill))).strip(" ,;-")
        key = fold(text)
        if not key or len(text) > 60 or key == title_key or key in out:
            continue
        out[key] = text
    return list(out.values())[:_MAX_SKILLS]


def normalize_locations(raw: Sequence[str], title: str) -> list[Location]:
    """Mỗi địa điểm thô → (tỉnh/thành mới, địa chỉ gốc). Nguồn không ghi địa điểm thì lấy tỉnh
    nêu trong tiêu đề ("... - Đồng Nai", "HN/HCM")."""
    out: list[Location] = []
    seen: set[tuple[str | None, str | None]] = set()
    for value in raw:
        address = _SPACES.sub(" ", ftfy.fix_text(value)).strip(" ,") if value else ""
        if not address:
            continue
        key = (provinces.province_of(address), address)
        if key not in seen:
            seen.add(key)
            out.append(Location(province=key[0], address_raw=address))
    if not out:
        out = [Location(province=p) for p in provinces.find_provinces(title)]
    return out


def normalize_job(raw: RawJob, *, now: datetime, board: bool = False) -> NormalizedJob:
    """Một tin thô → các trường đã chuẩn hoá (chưa xác minh, chưa gộp)."""
    fmt = raw.description_format
    title = clean_title(raw.title)
    description = clean_text_field(raw.description, fmt, raw.url)
    requirements = clean_text_field(raw.requirements, fmt, raw.url) or None
    benefits = clean_text_field(raw.benefits, fmt, raw.url) or None
    # Nguồn hay để cả JD trong mô tả (kèm mục yêu cầu/quyền lợi); "yêu cầu" vài chữ thường chỉ
    # là trình độ ("Đại học").
    if description and (requirements is None or len(requirements) < 50 or benefits is None):
        desc, req, ben = split_sections(description)
        if req is not None or ben is not None:
            description = desc
            if req is not None and (requirements is None or len(requirements) < 50):
                requirements = req if requirements is None else f"{req}\n\n- {requirements}"
            benefits = benefits or ben
    location_texts = [loc for loc in raw.locations if loc]
    locations = normalize_locations(location_texts, title)
    employment = classify_employment(raw.employment_type, title)
    level = classify_level(raw.level, title, raw.extra.get("occupational_category"))
    if level is JobLevel.UNKNOWN and employment is EmploymentType.INTERNSHIP:
        level = JobLevel.INTERN
    experience = parse_experience(raw.experience_raw) or parse_experience(
        requirements or description, require_context=True
    )
    salary = parse_salary(
        raw.salary_raw,
        low=raw.salary_min,
        high=raw.salary_max,
        currency=raw.salary_currency,
        period=raw.salary_period,
    )
    if salary is None:  # trang careers hay ghi lương trong nội dung tin
        salary = salary_in_text(description, benefits, requirements)
    return NormalizedJob(
        raw=raw,
        board=board,
        title=title,
        title_normalized=normalize_title(raw.title),
        description_md=description,
        requirements_md=requirements,
        benefits_md=benefits,
        level=level,
        employment_type=employment,
        work_mode=classify_work_mode(raw.work_mode, title, location_texts),
        locations=locations,
        salary=salary,
        experience_years=experience,
        skills=clean_skills(raw.skills, title),
        posted_at=raw.posted_at or parse_date(raw.posted_raw, now=now),
        expires_at=raw.expires_at or parse_date(raw.expires_raw, now=now, end_of_day=True),
        apply_url=raw.apply_url,
        places=provinces.most_specific(
            {u for loc in locations if (u := provinces.unit_of(loc.address_raw))}
            | set(provinces.find_units(title))
        ),
    )


def normalize_jobs(
    raws: Iterable[RawJob], *, now: datetime, is_board: Callable[[str], bool]
) -> list[NormalizedJob]:
    out: list[NormalizedJob] = []
    for raw in raws:
        try:
            out.append(normalize_job(raw, now=now, board=is_board(raw.source)))
        except Exception:  # một tin lạ không được làm hỏng cả doanh nghiệp
            log.exception("normalize.error", source=raw.source, url=raw.url)
    return out


# --- LLM cho trường mơ hồ -------------------------------------------------------------------------


def _apply_fields(job: NormalizedJob, value: job_fields.JobFields) -> bool:
    """Chỉ điền trường rule để `unknown` (không ghi đè rule), và chỉ khi trích đoạn bằng chứng
    LLM đưa ra có thật trong tin."""
    source = fold(" ".join([job.title, job_fields.known_fields(job.raw), job.jd_text]))

    def grounded(evidence: str | None) -> bool:
        quote = fold(evidence or "")
        return len(quote) >= 2 and quote in source

    changed = False
    if (
        job.level is JobLevel.UNKNOWN
        and value.level != "unknown"
        and grounded(value.level_evidence)
    ):
        job.level, changed = JobLevel(value.level), True
    if (
        job.employment_type is EmploymentType.UNKNOWN
        and value.employment_type != "unknown"
        and grounded(value.employment_type_evidence)
    ):
        job.employment_type, changed = EmploymentType(value.employment_type), True
    if (
        job.work_mode is WorkMode.UNKNOWN
        and value.work_mode != "unknown"
        and grounded(value.work_mode_evidence)
    ):
        job.work_mode, changed = WorkMode(value.work_mode), True
    return changed


FIELDS_CACHE_SECONDS = 30 * 86_400


async def classify_unclear(
    jobs: Sequence[NormalizedJob],
    llm: LLMClient,
    *,
    until: float,
    max_jobs: int,
    kv: KeyValueStore | None = None,
) -> int:
    """Hỏi LLM cấp bậc / loại hình / hình thức làm việc cho job rule chưa rõ (tối đa `max_jobs`
    lời gọi, dừng ở `until` theo `time.monotonic()`). Câu trả lời được cache theo nội dung tin
    (`kv`, 30 ngày), kể cả khi LLM trả unknown: chạy lại không hỏi lại tin không đổi.
    Trả số job được điền thêm."""
    todo = [j for j in jobs if j.unclear_fields]
    if not todo or not llm.enabled:
        return 0
    prompt, schema = job_fields.PROMPT, job_fields.JobFields
    filled = 0
    ask: list[NormalizedJob] = []
    cached = (
        await kv.mget([cache_key(prompt, j.content_hash) for j in todo])
        if kv
        else [None] * len(todo)
    )
    for job, raw in zip(todo, cached, strict=True):
        if raw is None:
            ask.append(job)
            continue
        try:
            filled += _apply_fields(job, schema.model_validate_json(raw))
        except ValueError:
            ask.append(job)
    hits = len(todo) - len(ask)
    ask = ask[:max_jobs]
    budget = until - time.monotonic()
    if not ask or budget <= 1:
        log.info("normalize.llm_fields", cached=hits, asked=0, filled=filled)
        return filled

    async def one(job: NormalizedJob) -> bool:
        result = await cached_structured(
            llm,
            kv,
            job.content_hash,
            prompt,
            schema,
            ttl=FIELDS_CACHE_SECONDS,
            title=job.title,
            fields=job_fields.known_fields(job.raw),
            unclear=", ".join(job.unclear_fields),
            jd=job.jd_text,
        )
        return result.value is not None and _apply_fields(job, result.value)

    tasks = [asyncio.ensure_future(one(j)) for j in ask]
    done, pending = await asyncio.wait(tasks, timeout=budget)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    filled += sum(1 for t in done if not t.cancelled() and t.exception() is None and t.result())
    log.info(
        "normalize.llm_fields",
        cached=hits,
        asked=len(ask),
        filled=filled,
        timed_out=len(pending),
    )
    return filled
