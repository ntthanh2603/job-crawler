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
