from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.exc import DBAPIError

from jobcrawler.common.schemas import CompanyIn
from jobcrawler.modules.companies import batch_service
from jobcrawler.modules.companies.batch_service import (
    CsvFormatError,
    ImportResult,
    clean_tax_code,
    parse_companies_csv,
)
from jobcrawler.pipeline import kyc


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
        batch_service.read_companies_csv(path)
    bom = tmp_path / "bom.csv"
    bom.write_bytes("name,website\nCông ty Hà Nội,hanoi.vn\n".encode("utf-8-sig"))
    [row] = batch_service.read_companies_csv(bom).rows
    assert row.company.name == "Công ty Hà Nội"


class _Stop(Exception):
    pass


async def test_import_locks_names_before_tax_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Khoá theo thứ tự của `kyc.resolve_company` (tên rồi `mst:`): xếp lẫn hai loại thì lô import
    giữ `mst:X` chờ tên "viettel" mà lượt chạy doanh nghiệp đang giữ (chờ `mst:X`) → deadlock."""
    keys: list[str] = []

    async def lock(session: Any, key: str) -> None:
        keys.append(key)

    class Session:
        async def scalars(self, stmt: Any) -> Any:
            raise _Stop  # đủ khoá rồi: không cần chạy tiếp

    monkeypatch.setattr(kyc, "_lock", lock)
    rows = [
        CompanyIn(name="Viettel", tax_code="0100109106"),
        CompanyIn(name="FPT Software", tax_code="0303490096"),
        CompanyIn(name="ACME"),
    ]
    with pytest.raises(_Stop):
        await batch_service._import_chunk(cast(Any, Session()), rows, ImportResult())  # pyright: ignore[reportPrivateUsage]
    assert keys == ["acme", "fpt software", "viettel", "mst:0100109106", "mst:0303490096"]


class _PgError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


class _Sessionmaker:
    @contextlib.asynccontextmanager
    async def begin(self) -> AsyncIterator[object]:
        yield object()


async def test_import_retries_chunk_after_deadlock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lô bị Postgres huỷ vì deadlock (với lượt KYC giữ dòng doanh nghiệp) chạy lại; số đếm của
    lần bị huỷ (đã rollback) không cộng vào kết quả."""
    states = ["40P01", None]

    async def chunk(session: Any, rows: Sequence[CompanyIn], result: ImportResult) -> None:
        result.created += len(rows)
        result.company_ids.append(cast(Any, rows[0].name))
        if (state := states.pop(0)) is not None:
            raise DBAPIError("SELECT pg_advisory_xact_lock(1)", None, _PgError(state))

    monkeypatch.setattr(batch_service, "_import_chunk", chunk)
    sessionmaker = cast(Any, _Sessionmaker())
    result = await batch_service.import_companies(sessionmaker, [CompanyIn(name="ACME")])
    assert (result.created, result.company_ids) == (1, ["ACME"])

    # Lỗi khác (vd. vi phạm ràng buộc) không chạy lại.
    states = ["23505"]
    with pytest.raises(DBAPIError):
        await batch_service.import_companies(sessionmaker, [CompanyIn(name="ACME")])
    # Deadlock mãi: thử IMPORT_DEADLOCK_RETRIES lần rồi báo lỗi.
    states = ["40P01"] * (batch_service.IMPORT_DEADLOCK_RETRIES + 1)
    with pytest.raises(DBAPIError):
        await batch_service.import_companies(sessionmaker, [CompanyIn(name="ACME")])
    assert states == []
