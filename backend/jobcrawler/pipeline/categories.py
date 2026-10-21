"""Ngành nghề của việc làm: một danh mục chung cho mọi nguồn và luật xếp tin vào danh mục.

Danh mục bám các nhóm ngành gốc của TopCV (trang tuyển dụng lớn nhất, ~25 nhóm) để đường danh
mục của TopCV khớp gọn một nhóm. Nguồn khác ghi ngành theo bộ chữ riêng ("IT phần mềm",
"Information Technology", "Bán Hàng Doanh Nghiệp - B2B", "Cơ khí - Ô tô - Tự động hóa"...): so
khớp cụm từ trên chuỗi đã `fold` (bỏ dấu, viết thường) như `pipeline.provinces`.

Ba loại đầu vào, theo độ tin cậy:

1. Đường danh mục phân cấp của nguồn (TopCV: gốc → nhánh → lá, `path_categories`): nhóm của gốc,
   thu hẹp dần theo cấp dưới ("Nhân sự/Hành chính/Pháp chế" → "Tuyển dụng" chỉ còn nhân sự).
2. Tiêu đề (`title_category`): một nhóm — cụm từ khớp sớm nhất (cùng chỗ thì cụm dài hơn: "kỹ sư
   thiết kế cơ khí" là cơ khí, không phải thiết kế). Cụm từ "yếu" (trợ lý, tư vấn...) chỉ dùng khi
   không có cụm nào khác.
3. Nhãn ngành phẳng của nguồn (`label_categories`: JSON-LD `industry`, phòng ban của ATS...): mọi
   nhóm khớp. Nhãn có thể là ngành của doanh nghiệp chứ không phải của vị trí (Glints,
   VietnamWorks) nên đứng sau tiêu đề; có đường danh mục thì bỏ qua.

Chỉ xét tiêu đề và nhãn, không đọc JD (JD nhắc "bảo hiểm", "môi trường làm việc"... ở mọi tin).

Vài từ viết tắt trùng chữ tiếng Việt khi bỏ dấu ("ít" / IT, "sắp" / SAP, "ủi" / UI, "ai" / AI,
"thuê" / thuế): chỉ nhận đúng cách viết của cụm từ (`_homograph`).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from jobcrawler.common.text import fold

MAX_CATEGORIES = 3


@dataclass(frozen=True, slots=True)
class Category:
    name: str
    # Cụm từ (đã fold) dùng cho cả tiêu đề và nhãn nguồn.
    phrases: tuple[str, ...]
    # Chỉ dùng cho tiêu đề khi không nhóm nào khớp cụm thường; nhãn nguồn vẫn dùng.
    weak: tuple[str, ...] = ()
    # Chỉ dùng cho nhãn nguồn: một mình trong tiêu đề thì quá chung / dễ nhầm.
    labels: tuple[str, ...] = ()
    # Cụm bị xoá trước khi so (nhãn "Điện thoại" không phải ngành điện).
    exclude: tuple[str, ...] = ()
    _strong: re.Pattern[str] | None = field(default=None, init=False, compare=False)
    _weak: re.Pattern[str] | None = field(default=None, init=False, compare=False)
    _any: re.Pattern[str] | None = field(default=None, init=False, compare=False)
    _exclude: re.Pattern[str] | None = field(default=None, init=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_strong", _pattern(self.phrases))
        object.__setattr__(self, "_weak", _pattern(self.weak))
        object.__setattr__(self, "_any", _pattern((*self.phrases, *self.weak, *self.labels)))
        object.__setattr__(self, "_exclude", _pattern(self.exclude))

    def prepare(self, text: str) -> str:
        """Che cụm loại trừ bằng ký tự không phải chữ (giữ nguyên vị trí các chữ còn lại)."""
        if self._exclude is None:
            return text
        return self._exclude.sub(lambda m: "#" * len(m.group()), text)

    def search(self, pattern: re.Pattern[str] | None, text: _Text) -> re.Match[str] | None:
        """Chỗ khớp đầu tiên của `pattern` (cụm thường / yếu / mọi cụm của nhóm) trong `text`, bỏ
        qua chỗ khớp chỉ là một từ đồng dạng ("ít" khớp cụm "it")."""
        if pattern is None:
            return None
        for m in pattern.finditer(self.prepare(text.folded)):
            if m.span() not in text.homographs:
                return m
        return None


def _pattern(phrases: Iterable[str]) -> re.Pattern[str] | None:
    # Cụm dài trước: cùng vị trí thì alternation lấy cụm dài hơn.
    alts = sorted({p for p in phrases if p}, key=len, reverse=True)
    if not alts:
        return None
    return re.compile(rf"(?<![0-9a-z])(?:{'|'.join(re.escape(p) for p in alts)})(?![0-9a-z])")


_WORD = re.compile(r"[^\W_]+")


def _homograph(word: str, folded: str) -> bool:
    """Từ chỉ trùng cụm từ khi bỏ dấu: "ít nhất" không phải IT, "sắp xếp" không phải SAP, "thợ
    ủi" không phải UI, "cho thuê" không phải thuế; "ai" (đại từ, không dấu) chỉ nhận viết hoa AI."""
    if folded in ("it", "sap", "ui"):
        return not word.isascii()
    if folded == "ai":
        return word != "AI"
    if folded == "thue":
        return word.lower() not in ("thue", "thuế")
    return False


@dataclass(frozen=True, slots=True)
class _Text:
    """Chuỗi đã `fold` và vị trí (đầu, cuối) các từ đồng dạng trong đó."""

    folded: str
    homographs: frozenset[tuple[int, int]] = frozenset()


def _fold(text: str) -> _Text:
    """`fold` theo từng từ, ghi lại vị trí các từ đồng dạng (`_homograph`)."""
    words: list[str] = []
    spans: set[tuple[int, int]] = set()
    pos = 0
    for word in _WORD.findall(unicodedata.normalize("NFC", text)):
        folded = fold(word)
        if not folded:
            continue
        if _homograph(word, folded):
            spans.add((pos, pos + len(folded)))
        words.append(folded)
        pos += len(folded) + 1
    return _Text(" ".join(words), frozenset(spans))


CATEGORIES: tuple[Category, ...] = (
    Category(
        "Kinh doanh / Bán hàng",
        (
            "kinh doanh",
            "ban hang",
            "sales",
            "sale",
            "telesales",
            "telesale",
            "presales",
            "pre sales",
            "business development",
            "phat trien kinh doanh",
            "phat trien thi truong",
            "account executive",
            "account manager",
            "key account",
            "thuc day ban",
            "b2b",
            "b2c",
        ),
    ),
    Category(
        "Marketing / PR / Quảng cáo",
        (
            "marketing",
            "pr",
            "quang cao",
            "truyen thong",
            "quan he cong chung",
            "content",
            "copywriter",
            "seo",
            "sem",
            "social media",
            "brand",
            "branding",
            "thuong hieu",
            "tiep thi",
            "su kien",
            "event",
            "kol",
            "koc",
            "advertising",
            "communications",
            "public relations",
        ),
    ),
    Category(
        "Chăm sóc khách hàng / Vận hành",
        (
            "cham soc khach hang",
            "cskh",
            "dich vu khach hang",
            "ho tro khach hang",
            "phuc vu khach hang",
            "customer service",
            "customer support",
            "customer success",
            "customer care",
            "call center",
            "contact center",
            "tong dai",
            "tong dai vien",
            "customer experience",
            "hau mai",
            "after sales",
            "trai nghiem khach hang",
        ),
        weak=("van hanh", "operation", "operations"),
    ),
    Category(
        "Nhân sự / Hành chính",
        (
            "nhan su",
            "hanh chinh",
            "tuyen dung",
            "hr",
            "hrbp",
            "human resources",
            "human resource",
            "recruitment",
            "recruiter",
            "talent acquisition",
            "c b",
            "payroll",
            "van thu",
            "le tan",
            "receptionist",
            "administrative",
            "organization development",
            "rh",
            "recursos humanos",
            "nhap lieu",
            "data entry",
        ),
        weak=("tro ly", "thu ky", "assistant", "secretary", "van phong"),
        labels=("admin",),
        exclude=("cho thue van phong", "van phong cho thue"),
    ),
    Category(
        "Luật / Pháp chế",
        (
            "luat",
            "luat su",
            "phap che",
            "phap ly",
            "legal",
            "lawyer",
            "cong chung",
            "tuan thu",
            "compliance",
        ),
    ),
    Category(
        "Kế toán / Kiểm toán / Thuế",
        (
            "ke toan",
            "kiem toan",
            "ke toan thue",
            "accounting",
            "accountant",
            "audit",
            "auditor",
            "tax",
            "kiem soat noi bo",
            "internal control",
        ),
        labels=("thue",),
    ),
    Category(
        "Tài chính / Ngân hàng / Bảo hiểm",
        (
            "tai chinh",
            "ngan hang",
            "bao hiem",
            "dau tu",
            "chung khoan",
            "tin dung",
            "giao dich vien",
            "quan ly quy",
            "quan tri rui ro",
            "tham dinh",
            "thu hoi no",
            "finance",
            "financial",
            "banking",
            "bank",
            "insurance",
            "investment",
            "credit",
            "teller",
            "nguon von",
            "thi truong von",
            "httd",
            "chong rua tien",
            "aml",
            "alm",
            "treasury",
            "capital market",
            "capital markets",
            "quan ly rui ro",
            "rui ro tin dung",
            "rui ro hoat dong",
            "operational risk",
            "credit risk",
            "kiem ngan",
            "bao lanh",
            "quan he khach hang ca nhan",
            "quan he khach hang doanh nghiep",
        ),
        weak=(
            "quan he khach hang",
            "khach hang ca nhan",
            "khach hang doanh nghiep",
            "khach hang uu tien",
            "thanh toan",
            "risk",
        ),
    ),
    Category(
        "Bất động sản",
        (
            "bat dong san",
            "bds",
            "nha dat",
            "real estate",
            "property",
            "moi gioi bat dong san",
            "cho thue van phong",
            "van phong cho thue",
        ),
        weak=("mat bang",),
    ),
    Category(
        "Xây dựng",
        (
            "xay dung",
            "kien truc",
            "kien truc su",
            "architect",
            "construction",
            "civil",
            "cong trinh",
            "thi cong",
            "ket cau",
            "shop drawing",
            "qs",
            "du toan",
            "trac dia",
            "trac dac",
            "cau duong",
            "co so ha tang",
            "ha tang ky thuat",
            "ha tang giao thong",
            "ha tang do thi",
            "thiet ke ket cau",
            "thiet ke kien truc",
            "thiet ke xay dung",
            "thiet ke ha tang",
            "ky su ha tang",
        ),
    ),
    Category(
        "Sản xuất / Cơ khí",
        (
            "san xuat",
            "manufacturing",
            "production",
            "nha may",
            "xuong",
            "co khi",
            "mechanical",
            "o to",
            "oto",
            "xe hoi",
            "automotive",
            "tu dong hoa",
            "automation",
            "che tao",
            "cnc",
            "khuon mau",
            "tho han",
            "welder",
            "welding",
            "qc",
            "quan ly chat luong",
            "kiem soat chat luong",
            "quality control",
            "det may",
            "may mac",
            "da giay",
            "hoa chat",
            "chemical",
            "thiet ke co khi",
            "thiet ke khuon",
            "van hanh may",
            "dam bao chat luong",
            "dbcl",
        ),
        weak=("bao tri", "bao duong", "maintenance"),
        labels=("qa", "thuc pham", "in an", "cong nghiep"),
    ),
    Category(
        "Điện / Điện tử / Viễn thông",
        (
            "dien tu",
            "vien thong",
            "dien lanh",
            "nhiet lanh",
            "dien cong nghiep",
            "dien dan dung",
            "dien nuoc",
            "dien nhe",
            "co dien",
            "ky su dien",
            "ky thuat dien",
            "tho dien",
            "he thong dien",
            "thiet ke dien",
            "thiet ke vi mach",
            "vi mach",
            "ban dan",
            "semiconductor",
            "electrical",
            "electronics",
            "electronic",
            "telecom",
            "telecommunication",
            "telecommunications",
            "hvac",
            "mep",
            "m e",
            "dieu hoa",
        ),
        labels=("dien",),
        exclude=("dien thoai", "dien may", "dien anh", "dai dien", "dien vien", "trinh dien"),
    ),
    Category(
        "Công nghệ thông tin",
        (
            "cong nghe thong tin",
            "cntt",
            "it",
            "information technology",
            "phan mem",
            "software",
            "developer",
            "devops",
            "lap trinh",
            "lap trinh vien",
            "programmer",
            "frontend",
            "front end",
            "backend",
            "back end",
            "fullstack",
            "full stack",
            "mobile developer",
            "ios",
            "android",
            "web developer",
            "lap trinh web",
            "data analyst",
            "data engineer",
            "data scientist",
            "big data",
            "phan tich du lieu",
            "khoa hoc du lieu",
            "machine learning",
            "ai",
            "ai engineer",
            "tester",
            "kiem thu",
            "software test",
            "automation test",
            "manual test",
            "qa engineer",
            "network",
            "mang may tinh",
            "quan tri mang",
            "ha tang mang",
            "system admin",
            "sysadmin",
            "system engineer",
            "database",
            "dba",
            "cloud",
            "erp",
            "sap",
            "helpdesk",
            "it helpdesk",
            "an ninh mang",
            "an toan thong tin",
            "bao mat thong tin",
            "cyber security",
            "cybersecurity",
            "information security",
            "network security",
            "security engineer",
            "embedded",
            "lap trinh nhung",
            "blockchain",
            "game developer",
            "product owner",
            "business analyst",
            "scrum master",
            "solution architect",
            "software architect",
            "cloud architect",
            "kien truc he thong",
            "kien truc phan mem",
            "computer",
            "may tinh",
            "phan cung",
            "java",
            "javascript",
            "python",
            "php",
            "golang",
            "nodejs",
            "reactjs",
            "flutter",
            "he thong thong tin",
            "algorithm",
            "purple team",
            "site reliability",
            "sre",
            "devsecops",
            "platform engineer",
            "infrastructure engineer",
            "enterprise architect",
            "data analytics",
            "data analysis",
            "business intelligence",
            "red team",
            "blue team",
            "pentest",
            "penetration test",
        ),
        weak=("data", "web", "du lieu", "dados"),
        labels=("internet", "engineering"),
    ),
    Category(
        "Lao động phổ thông",
        (
            "lao dong pho thong",
            "cong nhan",
            "tap vu",
            "giup viec",
            "bao ve",
            "ve sinh",
            "boc xep",
            "security guard",
            "an ninh",
        ),
    ),
    Category(
        "Logistics / Thu mua / Kho vận",
        (
            "logistics",
            "logistic",
            "xuat nhap khau",
            "xnk",
            "import export",
            "kho",
            "thu kho",
            "kho van",
            "warehouse",
            "van tai",
            "van chuyen",
            "giao nhan",
            "giao hang",
            "shipper",
            "forwarder",
            "freight",
            "chuoi cung ung",
            "supply chain",
            "thu mua",
            "mua hang",
            "purchasing",
            "procurement",
            "hai quan",
            "customs",
            "chung tu",
            "hang hai",
            "shipping",
            "logistica",
            "logistico",
            "transporte",
            "transportes",
            "inventario",
            "inventory",
            "hub",
            "last mile",
            "first mile",
            "line haul",
            "linehaul",
            "fulfillment",
            "fulfilment",
            "sorting",
            "lay hang",
            "soan hang",
            "picker",
            "packer",
            "dong goi",
            "logisticas",
            "mua sam",
            "cung ung",
        ),
        weak=("dau thau",),
        # "tư vấn tài chính", "tư vấn chuyển đổi số", "tư vấn tại…" bỏ dấu chứa "vấn tải/chuyển".
        exclude=("tu van tai", "tu van chuyen"),
    ),
    Category("Tài xế", ("tai xe", "lai xe", "driver", "motorista", "motoristas")),
    Category(
        "Giáo dục / Đào tạo",
        (
            "giao duc",
            "dao tao",
            "giao vien",
            "giang vien",
            "tro giang",
            "gia su",
            "mam non",
            "tuyen sinh",
            "giao vu",
            "hoc vu",
            "teacher",
            "tutor",
            "lecturer",
            "teaching",
            "education",
            "training",
            "trainer",
            "academic",
        ),
    ),
    Category(
        "Y tế / Dược / Sức khoẻ",
        (
            "y te",
            "duoc si",
            "duoc pham",
            "trinh duoc vien",
            "nganh duoc",
            "bac si",
            "dieu duong",
            "y ta",
            "ho ly",
            "suc khoe",
            "benh vien",
            "phong kham",
            "nha khoa",
            "nha si",
            "xet nghiem",
            "vat ly tri lieu",
            "phuc hoi chuc nang",
            "thiet bi y te",
            "cong nghe sinh hoc",
            "healthcare",
            "medical",
            "pharmacy",
            "pharmaceutical",
            "pharma",
            "pharmacist",
            "doctor",
            "nurse",
            "dental",
            "clinic",
            "hospital",
            "biotech",
            "biotechnology",
        ),
        labels=("duoc",),
    ),
    Category(
        "Thiết kế",
        (
            "thiet ke",
            "design",
            "designer",
            "do hoa",
            "graphic",
            "ui ux",
            "ui",
            "ux",
            "my thuat",
            "art",
            "artist",
            "hoa si",
            "illustrator",
            "animation",
            "animator",
            "2d",
            "trai nghiem nguoi dung",
            "user experience",
            "3d",
        ),
        labels=("noi that",),
    ),
    Category(
        "Báo chí / Truyền hình / Xuất bản",
        (
            "bao chi",
            "truyen hinh",
            "xuat ban",
            "bien tap",
            "bien tap vien",
            "phong vien",
            "nha bao",
            "journalist",
            "editor",
            "phim",
            "film",
            "dien anh",
            "quay phim",
            "dung phim",
            "video",
            "photographer",
            "nhiep anh",
            "mc",
            "phat thanh",
            "radio",
            "broadcast",
            "entertainment",
            "giai tri",
            "am nhac",
            "music",
            "san xuat phim",
            "san xuat video",
            "san xuat noi dung",
            "san xuat chuong trinh",
        ),
        labels=("media", "publishing"),
    ),
    Category(
        "Nhà hàng / Khách sạn / Du lịch",
        (
            "nha hang",
            "khach san",
            "du lich",
            "lu hanh",
            "tour",
            "f b",
            "fnb",
            "hospitality",
            "hotel",
            "restaurant",
            "resort",
            "travel",
            "tourism",
            "buong phong",
            "housekeeping",
            "dau bep",
            "bep",
            "phu bep",
            "bep truong",
            "chef",
            "cook",
            "pha che",
            "bartender",
            "barista",
            "phuc vu",
            "waiter",
            "waitress",
            "am thuc",
            "ca phe",
            "cafe",
            "coffee",
        ),
    ),
    Category(
        "Bán lẻ / Dịch vụ đời sống",
        (
            "ban le",
            "retail",
            "cua hang",
            "cua hang truong",
            "sieu thi",
            "thu ngan",
            "cashier",
            "store",
            "spa",
            "tham my",
            "lam dep",
            "beauty",
            "salon",
            "nail",
            "my pham",
            "the hinh",
            "gym",
            "fitness",
        ),
        labels=("hang tieu dung", "fmcg"),
    ),
    Category(
        "Năng lượng / Môi trường / Nông nghiệp",
        (
            "nang luong",
            "moi truong",
            "nong nghiep",
            "lam nghiep",
            "thuy san",
            "chan nuoi",
            "trong trot",
            "thu y",
            "bao ve thuc vat",
            "bao ve moi truong",
            "nong san",
            "dau khi",
            "khoang san",
            "dien mat troi",
            "nang luong tai tao",
            "xu ly nuoc",
            "nuoc thai",
            "energy",
            "environment",
            "environmental",
            "agriculture",
            "agricultural",
            "renewable",
            "solar",
            "oil and gas",
            "meio ambiente",
            "cay xanh",
        ),
    ),
    Category(
        "Biên phiên dịch",
        (
            "bien dich",
            "phien dich",
            "thong dich",
            "bien phien dich",
            "translator",
            "interpreter",
            "translation",
            "interpretation",
        ),
    ),
    Category(
        "Tư vấn",
        ("consulting", "consultant", "consultancy"),
        weak=("tu van", "tu van vien"),
    ),
)

CATEGORY_NAMES: list[str] = [c.name for c in CATEGORIES]
_BY_NAME = {c.name: c for c in CATEGORIES}
# Nhãn nguồn ghi nhiều ngành trong một chuỗi: "Xây dựng, Bất động sản", "Bảo hiểm,Ngân hàng".
_LABEL_SPLIT = re.compile(r"\s*[,;|\n]\s*")
# Nhãn dài hơn chừng này (một câu) hoặc có HTML: nguồn để mô tả vào ô phòng ban, không phải nhãn.
_MAX_LABEL = 60


def _matches(text: _Text) -> list[str]:
    """Mọi nhóm có cụm từ (kể cả cụm yếu, cụm chỉ cho nhãn) trong `text` đã fold."""
    return [c.name for c in CATEGORIES if c.search(c._any, text) is not None]


def label_categories(labels: Iterable[str]) -> list[str]:
    """Các nhóm của nhãn ngành phẳng (mỗi chuỗi có thể chứa nhiều nhãn cách nhau dấu phẩy)."""
    found: dict[str, None] = {}
    for value in labels:
        parts = _LABEL_SPLIT.split(value or "")
        if "<" in (value or "") or any(len(part) > _MAX_LABEL for part in parts):
            continue
        for label in parts:
            for name in _matches(_fold(label)):
                found.setdefault(name, None)
    return list(found)


def path_categories(path: Sequence[str]) -> list[str]:
    """Đường danh mục gốc → lá: nhóm của cấp cao nhất khớp được, thu hẹp theo cấp dưới khi cấp
    dưới khớp một phần ("Kinh doanh/Bán hàng" → "Sales IT Phần mềm" vẫn chỉ là kinh doanh)."""
    current: list[str] = []
    for label in path:
        names = _matches(_fold(label))
        if not names:
            continue
        if not current:
            current = names
            continue
        narrowed = [n for n in current if n in names]
        if narrowed:
            current = narrowed
    return current


def _title_match(title: str) -> tuple[str, bool] | None:
    """(nhóm, khớp cụm thường) của tiêu đề: cụm từ khớp sớm nhất, cùng vị trí thì cụm dài hơn;
    không có cụm thường nào thì xét cụm yếu."""
    text = _fold(title)
    if not text.folded:
        return None
    for strength in ("_strong", "_weak"):
        best: tuple[int, int, str] | None = None
        for c in CATEGORIES:
            m = c.search(getattr(c, strength), text)
            if m is None:
                continue
            key = (m.start(), -(m.end() - m.start()), c.name)
            if best is None or key < best:
                best = key
        if best is not None:
            return best[2], strength == "_strong"
    return None


def title_category(title: str) -> str | None:
    """Nhóm của tiêu đề (cụm yếu chỉ khi không có cụm thường)."""
    match = _title_match(title)
    return match[0] if match else None


def classify_categories(
    title: str, *, path: Sequence[str] = (), labels: Sequence[str] = ()
) -> list[str]:
    """Ngành nghề của một tin (≤ MAX_CATEGORIES, ngành chính trước): đường danh mục của nguồn,
    rồi tiêu đề, rồi nhãn ngành phẳng (bỏ qua khi đã có đường danh mục). Tiêu đề chỉ khớp cụm yếu
    ("tư vấn viên", "trợ lý") thì chỉ dùng khi nguồn không ghi ngành."""
    from_path = path_categories(path)
    source = from_path or label_categories(labels)
    match = _title_match(title)
    named = match[0] if match and (match[1] or not source) else None
    ordered = [*source, named] if from_path else [named, *source]
    return list(dict.fromkeys(c for c in ordered if c))[:MAX_CATEGORIES]


def merge_categories(lists: Iterable[Sequence[str]]) -> list[str]:
    """Gộp ngành của nhiều tin cùng một việc (thứ tự theo tin, ≤ MAX_CATEGORIES)."""
    return list(dict.fromkeys(c for names in lists for c in names if c in _BY_NAME))[
        :MAX_CATEGORIES
    ]


def strings(value: Any) -> list[str]:
    """Giá trị nhãn trong `RawJob.extra` (chuỗi, danh sách chuỗi / object có `name`) → danh sách."""
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, dict):
        return strings(value.get("name") or value.get("label"))
    if isinstance(value, list | tuple):
        return [s for v in value for s in strings(v)]
    return []
