from __future__ import annotations

from pathlib import Path

import pytest

from jobcrawler.db.models import Company
from jobcrawler.services import batch
from jobcrawler.services.batch import CsvFormatError, clean_tax_code, parse_companies_csv


def test_parse_spec_columns() -> None:
    report = parse_companies_csv(
        "name,tax_code,website,note\n"
        "Công ty Cổ phần VNG,0303490096,vng.com.vn,khách VIP\n"
        "KiotViet,,https://www.kiotviet.vn/tuyen-dung,\n"
        "Chỉ có tên,,,\n"
    )
    assert [r.line for r in report.rows] == [2, 3, 4]
    vng, kiot, bare = (r.company for r in report.rows)
    assert (vng.name, vng.tax_code, vng.website, vng.note) == (
        "Công ty Cổ phần VNG",
        "0303490096",
        "vng.com.vn",
        "khách VIP",
    )
    assert kiot.website == "https://www.kiotviet.vn/tuyen-dung"
    assert (bare.tax_code, bare.website, bare.note) == (None, None, None)
    assert report.errors == report.warnings == []


def test_parse_excel_vietnamese_headers_and_semicolons() -> None:
    report = parse_companies_csv(
        "Tên doanh nghiệp;MST;Website;Ghi chú\n"
        "  Ngân hàng   TMCP Á Châu ;301452948;acb.com.vn;\n"  # Excel làm mất số 0 đầu
        "Chi nhánh ABC;0101234567001;;\n"
        ";0101234567;abc.vn;\n"  # thiếu tên
        "\n"
        "XYZ;12ab;không phải web;\n"
    )
    acb, branch, xyz = (r.company for r in report.rows)
    assert acb.name == "Ngân hàng TMCP Á Châu"
    assert acb.tax_code == "0301452948"
    assert branch.tax_code == "0101234567-001"
    assert (xyz.tax_code, xyz.website) == (None, None)
    assert report.errors == ["dòng 4: thiếu tên doanh nghiệp"]
    assert report.warnings == [
        "dòng 2: MST '301452948' thiếu số 0 đầu (Excel?), dùng 0301452948",
        "dòng 6: MST '12ab' không hợp lệ, bỏ qua",
        "dòng 6: website 'không phải web' không hợp lệ, bỏ qua",
    ]


def test_duplicate_rows_are_dropped() -> None:
    report = parse_companies_csv(
        "name,tax_code\n"
        "Công ty TNHH ABC,\n"
        '"ABC Co., Ltd",\n'  # cùng tên chuẩn hoá
        "Công ty CP ABC,0101234567\n"  # khác MST: pháp nhân khác
        "Công ty cổ phần ABC,0101234567\n"
    )
    assert [r.company.name for r in report.rows] == ["Công ty TNHH ABC", "Công ty CP ABC"]
    assert report.duplicates == 2


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0101234567", "0101234567"),
        ("0101 234 567", "0101234567"),
        ("0101234567-001", "0101234567-001"),
        ("0101234567 – 001", "0101234567-001"),
        ("101234567", "0101234567"),
        ("101234567001", "0101234567-001"),
        ("123", None),
        ("", None),
        (None, None),
    ],
)
def test_clean_tax_code(value: str | None, expected: str | None) -> None:
    assert clean_tax_code(value)[0] == expected


def test_header_and_encoding_errors(tmp_path: Path) -> None:
    with pytest.raises(CsvFormatError, match="thiếu cột `name`"):
        parse_companies_csv("company_id,tax_code\n1,0101234567\n")
    with pytest.raises(CsvFormatError, match="rỗng"):
        parse_companies_csv("")
    path = tmp_path / "ansi.csv"
    path.write_bytes("name\nCông ty Hà\n".encode("cp1252"))
    with pytest.raises(CsvFormatError, match="UTF-8"):
        batch.read_companies_csv(path)
    bom = tmp_path / "bom.csv"
    bom.write_bytes("name,website\nCông ty Hà Nội,hanoi.vn\n".encode("utf-8-sig"))
    [row] = batch.read_companies_csv(bom).rows
    assert row.company.name == "Công ty Hà Nội"


def test_job_key_separates_legal_entities_with_same_name() -> None:
    a = Company(id=1, input_name="Công ty TNHH ABC", tax_code=None)
    b = Company(id=2, input_name="Công ty Cổ phần ABC", tax_code="0101234567")
    assert batch.job_key(a) == "company:abc"
    assert batch.job_key(b) == "company:abc:0101234567"
    assert batch.job_kwargs(b) == {
        "company_id": 2,
        "name": "Công ty Cổ phần ABC",
        "tax_code": "0101234567",
        "website": None,
        "note": None,
    }
