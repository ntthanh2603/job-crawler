"""Tiện ích chuẩn hoá văn bản dùng chung."""

from __future__ import annotations

import re
import unicodedata

_NON_ALNUM = re.compile(r"[^0-9a-z]+")

# Loại hình pháp lý, viết thường và đã bỏ dấu. Bỏ ở bất kỳ vị trí nào trong tên vì tiếng Việt
# đặt ở đầu ("Công ty TNHH ABC") còn tiếng Anh đặt ở cuối ("ABC Co., Ltd").
_LEGAL_PHRASES: tuple[tuple[str, ...], ...] = tuple(
    sorted(
        (
            tuple(p.split())
            for p in (
                "cong ty trach nhiem huu han",
                "trach nhiem huu han",
                "cong ty co phan",
                "co phan",
                "cong ty cp",
                "ctcp",
                "cong ty tnhh",
                "tnhh",
                "tong cong ty",
                "cong ty",
                "tap doan",
                "mot thanh vien",
                "hai thanh vien tro len",
                "hai thanh vien",
                "mtv",
                "joint stock company",
                "joint stock",
                "jsc",
                "company limited",
                "co ltd",
                "company",
                "limited",
                "ltd",
                "llc",
                "inc",
                "incorporated",
                "corporation",
                "corp",
                "plc",
                "pte",
                "gmbh",
            )
        ),
        key=len,
        reverse=True,
    )
)


def strip_accents(text: str) -> str:
    """Bỏ dấu tiếng Việt: "Đà Nẵng" → "Da Nang"."""
    text = text.replace("đ", "d").replace("Đ", "D")
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _tokens(text: str) -> list[str]:
    return _NON_ALNUM.sub(" ", strip_accents(text).lower()).split()


def fold(text: str) -> str:
    """Viết thường, bỏ dấu, chỉ giữ chữ và số (cách nhau một dấu cách): dùng để so chuỗi."""
    return " ".join(_tokens(text))


# Khoá map profile trên trang tuyển dụng (`company_match_key`).
_PARENS = re.compile(r"\([^)]*\)|\[[^\]]*\]")
# "Ngân hàng TMCP Quân Đội - Chi nhánh Bắc Ninh": tin của chi nhánh là tin của doanh nghiệp.
_BRANCH = re.compile(r" (chi nhanh|van phong dai dien|branch office|branch) .*$")
_ABBREVIATIONS = {
    "tmcp": "thuong mai co phan",
    "nhtmcp": "ngan hang thuong mai co phan",
}
# Đuôi chung chung: "Shopee Việt Nam", "Viettel Group".
_GENERIC_TAILS: tuple[tuple[str, ...], ...] = (
    ("viet", "nam"),
    ("vietnam",),
    ("vn",),
    ("group",),
    ("holdings",),
    ("holding",),
)
# Từ của tên ngành nghề: phần còn lại chỉ gồm các từ này thì giữ đuôi ("Hàng không Việt Nam").
_GENERIC_WORDS = frozenset(
    {
        "ngan", "hang", "thuong", "mai", "dich", "vu", "xay", "dung", "san", "xuat", "dau",
        "tu", "cong", "nghe", "phat", "trien", "khong", "bao", "hiem", "tai", "chinh", "bat",
        "dong", "van", "du", "lich", "giao", "duc", "thuc", "pham", "duoc", "y", "te", "truyen",
        "thong", "quoc", "ky", "thuat", "thiet", "bi", "vat", "nhap", "khau", "kinh", "doanh",
        "dien", "may", "moi", "truong", "nong", "nghiep", "the", "and", "of", "bank",
        "software", "technology", "technologies", "tech", "solutions", "services", "trading",
        "investment", "insurance", "logistics", "airlines", "national",
    }
)  # fmt: skip


def company_match_key(name: str) -> str:
    """Khoá CHỈ dùng để map profile trên trang tuyển dụng ↔ doanh nghiệp
    (`source_profiles.name_norm`, alias `kind="match"`); KYC và gộp doanh nghiệp vẫn dùng
    `normalize_company_name`.

    Như `normalize_company_name` nhưng bỏ phần trong ngoặc và phần chi nhánh, mở rộng "TMCP"
    trước khi bỏ loại hình pháp lý, bỏ đuôi chung chung ("Việt Nam", "Group") khi phần còn lại
    vẫn có từ riêng: "Ngân hàng TMCP Quân Đội" và "Ngân Hàng Thương Mại Cổ Phần Quân Đội (MB
    Bank)" cùng khoá, "Shopee Việt Nam" ~ "Shopee"; "FPT" và "FPT Software" vẫn khác khoá."""
    folded = _BRANCH.sub("", fold(_PARENS.sub(" ", name)))
    expanded = " ".join(_ABBREVIATIONS.get(t, t) for t in folded.split())
    words = normalize_company_name(expanded).split()
    trimmed = True
    while trimmed:
        trimmed = False
        for tail in _GENERIC_TAILS:
            rest = words[: -len(tail)]
            if (
                len(words) > len(tail)
                and tuple(words[-len(tail) :]) == tail
                and any(len(w) >= 3 and w not in _GENERIC_WORDS for w in rest)
            ):
                words, trimmed = rest, True
                break
    return " ".join(words) or normalize_company_name(name)


def normalize_company_name(name: str) -> str:
    """Tên dùng để so khớp (`company_aliases.alias_norm`).

    Viết thường, bỏ dấu, bỏ dấu câu và loại hình pháp lý. Nếu tên chỉ gồm loại hình pháp lý
    thì giữ nguyên các token để không trả về chuỗi rỗng.
    """
    tokens = _tokens(name)
    kept: list[str] = []
    i = 0
    while i < len(tokens):
        for phrase in _LEGAL_PHRASES:
            if tuple(tokens[i : i + len(phrase)]) == phrase:
                i += len(phrase)
                break
        else:
            kept.append(tokens[i])
            i += 1
    return " ".join(kept or tokens)
