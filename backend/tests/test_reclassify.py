"""`jobcrawler db reclassify` tính như lúc chạy doanh nghiệp (`normalize_job` + `dedup.merge`)."""

from __future__ import annotations

from typing import Any

from jobcrawler.modules.maintenance.maintenance_service import reclassified

from .jobs_fixtures import NOW, raw_job

TITLE = "Kế toán tổng hợp"


def raws() -> tuple[dict[str, Any], dict[str, Any]]:
    # Tin chuẩn: không có trường học vấn, JD ghi "Tốt nghiệp đại học"; tin khác: trường học vấn
    # "Cao đẳng", JD không nói gì.
    canonical = raw_job().model_dump(mode="json")
    other = raw_job(
        source="topcv",
        external_id="acme-2",
        url="https://www.topcv.vn/viec-lam/ke-toan-tong-hop/2.html",
        description="<p>Hạch toán chứng từ kế toán hằng ngày, lập báo cáo thuế định kỳ.</p>",
        extra={"education": "Cao đẳng trở lên", "openings": 3},
    ).model_dump(mode="json")
    return canonical, other


def test_education_of_canonical_posting_wins_like_the_pipeline() -> None:
    """Lúc chạy, học vấn lấy của tin chuẩn (trường nguồn, rồi JD của chính nó); tin khác chỉ điền
    khi tin chuẩn chưa rõ. Đọc trường của mọi tin trước JD thì ra "cao đẳng" và lượt chạy sau lại
    đổi về "đại học"."""
    canonical, other = raws()
    values = reclassified(TITLE, None, "", [canonical, other], now=NOW)
    assert values == {
        "categories": ["Kế toán / Kiểm toán / Thuế"],
        "education": "bachelor",
        "openings": 3,  # tin chuẩn không ghi: lấy của tin khác như `dedup.merge`
    }
    # Tin kia là bản chuẩn: trường học vấn của nó.
    assert reclassified(TITLE, None, "", [other, canonical], now=NOW)["education"] == "college"


def test_no_postings_left_falls_back_to_stored_text() -> None:
    values = reclassified(TITLE, "Tốt nghiệp cao đẳng trở lên", "", [{"title": "thiếu trường"}])
    assert values == {
        "categories": ["Kế toán / Kiểm toán / Thuế"],
        "education": "college",
        "openings": None,
    }
