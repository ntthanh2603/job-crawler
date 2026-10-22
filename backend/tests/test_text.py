from __future__ import annotations

import pytest

from jobcrawler.common.text import (
    company_match_key,
    distinctive_key,
    normalize_company_name,
    strip_accents,
)


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


@pytest.mark.parametrize(
    ("a", "b"),
    [
        # TMCP viết tắt / viết đủ, tên thương hiệu trong ngoặc.
        ("Ngân hàng TMCP Quân Đội", "Ngân Hàng Thương Mại Cổ Phần Quân Đội (MB Bank)"),
        # Profile của chi nhánh.
        ("Ngân hàng TMCP Quân Đội", "Ngân hàng TMCP Quân Đội - Chi nhánh Bắc Ninh"),
        # Đuôi chung chung.
        ("Shopee", "Shopee Việt Nam"),
        ("Viettel", "Viettel Group"),
        ("Công ty TNHH Yakult Việt Nam", "Yakult"),
    ],
)
def test_company_match_key_joins_name_variants(a: str, b: str) -> None:
    assert company_match_key(a) == company_match_key(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("FPT", "FPT Software"),
        ("Samsung Electronics Việt Nam", "Samsung Electronics Việt Nam Thái Nguyên"),
        ("Hàng không Việt Nam", "Hàng không Vietjet"),
        ("Công ty Xây dựng số 1", "Công ty Xây dựng số 2"),
    ],
)
def test_company_match_key_keeps_different_companies_apart(a: str, b: str) -> None:
    assert company_match_key(a) != company_match_key(b)


def test_company_match_key_keeps_generic_names() -> None:
    # Phần còn lại toàn từ chung ngành: giữ đuôi "Việt Nam".
    assert company_match_key("Tổng công ty Hàng không Việt Nam") == "hang khong viet nam"
    assert company_match_key("Ngân hàng TMCP Ngoại thương Việt Nam (Vietcombank)") == (
        "ngan hang thuong mai ngoai thuong"
    )


def test_legal_forms() -> None:
    from jobcrawler.common.text import legal_forms, same_legal_entity_form

    assert legal_forms("Ngân hàng TMCP Quân đội (MB)") == {"cp"}
    assert legal_forms("Tổng Công ty Cổ phần Bảo Minh") == {"tct", "cp"}
    assert legal_forms("ABC Co., Ltd") == {"tnhh"}
    assert legal_forms("Bonbanh.com") == set()
    assert not same_legal_entity_form(
        "Công ty TNHH Hòa Phát", ["Công ty Cổ phần Tập đoàn Hòa Phát"]
    )
    assert not same_legal_entity_form(
        "Công ty TNHH Thương mại Hà Nội Việt Nam", ["Tổng Công ty Thương mại Hà Nội (Hapro)"]
    )
    assert same_legal_entity_form("Sunhouse Group JSC", ["Công ty Cổ phần Tập đoàn Sunhouse"])
    assert same_legal_entity_form("Sunhouse", ["Công ty Cổ phần Tập đoàn Sunhouse"])


def test_distinctive_key() -> None:
    assert distinctive_key("kiotviet")
    assert distinctive_key("yakult")
    assert not distinctive_key("thuong mai dich vu")  # toàn từ chung ngành
    assert not distinctive_key("abc")  # quá ngắn
