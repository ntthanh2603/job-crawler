"""Danh mục 34 tỉnh/thành sau sáp nhập (Nghị quyết 202/2025/QH15, hiệu lực 1/7/2025) và bảng ánh
xạ tên cũ → tên mới.

Từ 7/2025 không còn cấp huyện: địa chỉ chỉ chuẩn hoá tới tỉnh/thành; phần còn lại giữ ở
`address_raw`. So khớp trên chuỗi đã bỏ dấu, viết thường (`text.fold`) nên "Hà Nội", "Ha Noi",
"HN", "Hanoi" ra cùng một giá trị.

Tên đường trùng tên tỉnh/thành ("72 Nguyễn Huệ", "235 Điện Biên Phủ", "Xa lộ Hà Nội") không tính
là tỉnh/thành của địa chỉ (`_street_name`).
"""

from __future__ import annotations

import re
from functools import cache

from jobcrawler.common.text import fold

# Tên chuẩn → cách viết khác (đã bỏ dấu, viết thường; tên chuẩn tự được thêm).
PROVINCES: dict[str, tuple[str, ...]] = {
    # 11 đơn vị giữ nguyên.
    "Hà Nội": ("hn", "hanoi", "tp ha noi", "thanh pho ha noi"),
    "Huế": ("thua thien hue", "tt hue", "thanh pho hue"),
    "Lai Châu": (),
    "Điện Biên": ("dien bien phu",),
    "Sơn La": (),
    "Lạng Sơn": (),
    "Quảng Ninh": ("ha long", "halong", "cam pha", "mong cai", "uong bi"),
    "Thanh Hóa": (),
    "Nghệ An": (),
    "Hà Tĩnh": (),
    "Cao Bằng": (),
    # 23 đơn vị mới (tên cũ được gộp nằm ở OLD_PROVINCES).
    "Tuyên Quang": (),
    "Lào Cai": ("sa pa", "sapa"),
    "Thái Nguyên": (),
    "Phú Thọ": ("viet tri",),
    "Bắc Ninh": ("tu son",),
    "Hưng Yên": (),
    "Hải Phòng": ("haiphong",),
    "Ninh Bình": (),
    "Quảng Trị": (),
    "Đà Nẵng": ("danang",),
    "Quảng Ngãi": (),
    "Gia Lai": ("pleiku",),
    "Khánh Hòa": ("nha trang", "cam ranh"),
    "Lâm Đồng": ("da lat", "dalat", "bao loc"),
    "Đắk Lắk": ("daklak", "dac lac", "buon ma thuot", "buon me thuot"),
    "Hồ Chí Minh": (
        "hcm",
        "hcmc",
        "tp hcm",
        "tphcm",
        "tp ho chi minh",
        "thanh pho ho chi minh",
        "ho chi minh city",
        "sai gon",
        "saigon",
    ),
    "Đồng Nai": ("bien hoa",),
    "Tây Ninh": (),
    "Cần Thơ": ("cantho",),
    "Vĩnh Long": (),
    "Đồng Tháp": ("cao lanh",),
    "Cà Mau": (),
    "An Giang": ("long xuyen", "chau doc"),
}

# Tỉnh/thành cũ (trước 7/2025) → tỉnh/thành mới, kèm cách viết khác của tên cũ.
OLD_PROVINCES: dict[str, tuple[str, tuple[str, ...]]] = {
    "Hà Giang": ("Tuyên Quang", ()),
    "Yên Bái": ("Lào Cai", ()),
    "Bắc Kạn": ("Thái Nguyên", ("bac can",)),
    "Vĩnh Phúc": ("Phú Thọ", ("vinh yen", "phuc yen")),
    "Hòa Bình": ("Phú Thọ", ()),
    "Bắc Giang": ("Bắc Ninh", ()),
    "Thái Bình": ("Hưng Yên", ()),
    "Hải Dương": ("Hải Phòng", ()),
    "Hà Nam": ("Ninh Bình", ("phu ly",)),
    "Nam Định": ("Ninh Bình", ()),
    "Quảng Bình": ("Quảng Trị", ("dong hoi",)),
    "Quảng Nam": ("Đà Nẵng", ("hoi an", "tam ky")),
    "Kon Tum": ("Quảng Ngãi", ("kontum",)),
    "Bình Định": ("Gia Lai", ("quy nhon",)),
    "Ninh Thuận": ("Khánh Hòa", ("phan rang",)),
    "Đắk Nông": ("Lâm Đồng", ("dak nong", "dac nong")),
    "Bình Thuận": ("Lâm Đồng", ("phan thiet",)),
    "Phú Yên": ("Đắk Lắk", ("tuy hoa",)),
    "Bình Dương": ("Hồ Chí Minh", ("thu dau mot", "di an", "thuan an")),
    "Bà Rịa - Vũng Tàu": ("Hồ Chí Minh", ("ba ria", "vung tau", "brvt", "ba ria vung tau")),
    "Bình Phước": ("Đồng Nai", ()),
    "Long An": ("Tây Ninh", ()),
    "Tiền Giang": ("Đồng Tháp", ("my tho",)),
    "Bến Tre": ("Vĩnh Long", ()),
    "Trà Vinh": ("Vĩnh Long", ()),
    "Sóc Trăng": ("Cần Thơ", ()),
    "Hậu Giang": ("Cần Thơ", ()),
    "Kiên Giang": ("An Giang", ("rach gia", "phu quoc")),
    "Bạc Liêu": ("Cà Mau", ()),
}


@cache
def _aliases() -> dict[str, str]:
    """Cách viết (đã fold) → tên tỉnh/thành mới."""
    table: dict[str, str] = {}
    for name, others in PROVINCES.items():
        for alias in (fold(name), *others):
            table[alias] = name
    for old, (new, others) in OLD_PROVINCES.items():
        assert new in PROVINCES, new
        for alias in (fold(old), *others):
            table.setdefault(alias, new)
    return table


@cache
def _units() -> dict[str, str]:
    """Cách viết (đã fold) → tên đơn vị như được viết: tên cũ nếu là tỉnh cũ ("Hải Dương"),
    không thì tên mới. Dùng để phân biệt hai nơi cùng tỉnh mới nhưng khác tỉnh cũ."""
    table = {alias: name for alias, name in _aliases().items()}
    for old, (_, others) in OLD_PROVINCES.items():
        for alias in (fold(old), *others):
            table[alias] = old
    return table


@cache
def _pattern() -> re.Pattern[str]:
    # Cách viết dài trước để "ba ria vung tau" thắng "ba ria"; ranh giới từ hai phía.
    aliases = sorted(_aliases(), key=len, reverse=True)
    return re.compile(r"(?<![0-9a-z])(?:" + "|".join(map(re.escape, aliases)) + r")(?![0-9a-z])")


# Phần của địa chỉ: "Tầng 5, 72 Nguyễn Huệ, Quận 1" → "Tầng 5" / "72 Nguyễn Huệ" / "Quận 1".
_PARTS = re.compile(r"[,;|\n]|\s[-–]\s")
# Trước tên đường: số nhà đầu phần địa chỉ ("72 Nguyễn Huệ", "Số 12A/3 Điện Biên Phủ"; một chữ xen
# giữa cho tên người như "Nguyễn Huệ"), chữ "đường" / "phố" / "xa lộ"..., hoặc họ "Nguyễn".
_STREET_BEFORE = re.compile(
    r"(?:^(?:so\s+)?\d+[a-z]?(?:\s\d+[a-z]?)*\s(?:[a-z]+\s)?"
    r"|\b(?:duong|(?<!thanh )pho|ngo|hem|kiet|xa lo|dai lo|quoc lo|tinh lo|nguyen)\s)$"
)


def _street_name(part: str, m: re.Match[str]) -> bool:
    """Chỗ khớp tên tỉnh/thành trong một phần địa chỉ (đã fold) là tên đường."""
    return _STREET_BEFORE.search(part[: m.start()]) is not None


def _address_matches(address: str) -> list[str]:
    """Cách viết tỉnh/thành (đã fold) nhắc tới trong địa chỉ, theo thứ tự, bỏ tên đường."""
    found: list[str] = []
    for part in _PARTS.split(address):
        folded = fold(part)
        found += [m.group(0) for m in _pattern().finditer(folded) if not _street_name(folded, m)]
    return found


# Phần còn lại của địa chỉ chỉ gồm tỉnh/thành: chữ nối, "TP", "tỉnh", tên nước.
_LIST_FILLER = re.compile(
    r"\b(?:thanh pho|tp|tinh|khu vuc|chi nhanh|cn|va|and|hoac|or|viet nam|vietnam|vn)\b"
)


def list_units(address: str | None) -> list[str]:
    """Địa chỉ chỉ là danh sách tỉnh/thành ("Hà Nội, Hồ Chí Minh", "HN/HCM", "Hà Nội và Đà Nẵng")
    → các đơn vị (giữ tên tỉnh cũ, như `find_units`); địa chỉ thật (có đường, phường...) → []."""
    if not address:
        return []
    folded = fold(address)
    if _LIST_FILLER.sub(" ", _pattern().sub(" ", folded)).strip():
        return []
    return find_units(address)


def find_provinces(text: str | None) -> list[str]:
    """Mọi tỉnh/thành (tên mới) nhắc tới trong `text`, theo thứ tự xuất hiện, không lặp."""
    if not text:
        return []
    table = _aliases()
    found = [table[m.group(0)] for m in _pattern().finditer(fold(text))]
    return list(dict.fromkeys(found))


def province_of(address: str | None) -> str | None:
    """Tỉnh/thành của một địa chỉ: tên nhắc tới sau cùng (địa chỉ Việt Nam ghi tỉnh ở cuối,
    "phường Vĩnh Phúc, Hà Nội" là Hà Nội), bỏ tên đường ("72 Nguyễn Huệ, Quận 1" không phải
    Huế)."""
    if not address:
        return None
    matches = _address_matches(address)
    return _aliases()[matches[-1]] if matches else None


def find_units(text: str | None) -> list[str]:
    """Như `find_provinces` nhưng giữ tên tỉnh cũ: "Hải Dương" → "Hải Dương" (không phải
    "Hải Phòng")."""
    if not text:
        return []
    table = _units()
    return list(dict.fromkeys(table[m.group(0)] for m in _pattern().finditer(fold(text))))


def unit_of(address: str | None) -> str | None:
    """Như `province_of` nhưng giữ tên tỉnh cũ."""
    if not address:
        return None
    matches = _address_matches(address)
    return _units()[matches[-1]] if matches else None


def most_specific(units: set[str]) -> frozenset[str]:
    """Bỏ tên tỉnh mới khi đã có tên tỉnh cũ nằm trong nó: {"Bình Thuận", "Lâm Đồng"} →
    {"Bình Thuận"} (tin ghi "Bình Thuận| ..." với địa chỉ "..., tỉnh Lâm Đồng")."""
    covered = {OLD_PROVINCES[u][0] for u in units if u in OLD_PROVINCES}
    return frozenset(u for u in units if u not in covered)


def province_pattern() -> re.Pattern[str]:
    """Regex (trên chuỗi đã fold) khớp mọi cách viết tên tỉnh/thành."""
    return _pattern()
