from __future__ import annotations

import pytest

from jobcrawler.text import normalize_company_name, strip_accents


def test_strip_accents() -> None:
    assert strip_accents("Đà Nẵng, Hồ Chí Minh") == "Da Nang, Ho Chi Minh"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Công ty TNHH ABC", "abc"),
        ("CÔNG TY CỔ PHẦN FPT", "fpt"),
        ("FPT JSC", "fpt"),
        ("ABC Co., Ltd", "abc"),
        ("ABC Company Limited", "abc"),
        ("Công ty TNHH MTV Thương mại Đông Á", "thuong mai dong a"),
        ("Công ty Trách nhiệm hữu hạn Một thành viên XYZ", "xyz"),
        ("CTCP Đầu tư Hà Nội", "dau tu ha noi"),
        ("Tập đoàn Vingroup - CTCP", "vingroup"),
        ("Shopee Vietnam Co.,Ltd.", "shopee vietnam"),
        ("  Test  ", "test"),
        # Chỉ có loại hình pháp lý: giữ nguyên thay vì trả chuỗi rỗng.
        ("Công ty TNHH", "cong ty tnhh"),
        # Không bỏ từ trùng một phần với loại hình pháp lý.
        ("Coca-Cola Beverages Vietnam", "coca cola beverages vietnam"),
        ("Incomm Vietnam", "incomm vietnam"),
    ],
)
def test_normalize_company_name(name: str, expected: str) -> None:
    assert normalize_company_name(name) == expected


def test_variants_share_alias_norm() -> None:
    variants = ["Công ty Cổ phần Tiki", "TIKI JSC", "tiki corporation", "Tiki"]
    assert {normalize_company_name(v) for v in variants} == {"tiki"}
