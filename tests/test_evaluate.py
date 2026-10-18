from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from jobcrawler.services import evaluate
from jobcrawler.services.evaluate import (
    CompanyEval,
    EvalReport,
    GoldenError,
    GoldenJob,
    Predicted,
    SourceEval,
)


def golden(title: str | None = None, url: str | None = None) -> GoldenJob:
    return GoldenJob(line=2, company="ACME", url=url, title=title)


def predicted(title: str, *urls: str, source: str = "careers") -> Predicted:
    return Predicted(
        title=title, urls={k for u in urls if (k := evaluate.url_key(u))}, source=source
    )


def test_url_key_ignores_tracking_and_trailing_slash() -> None:
    a = evaluate.url_key("https://ACME.vn/jobs/123/?utm_source=fb")
    assert a == evaluate.url_key("https://acme.vn/jobs/123")
    assert evaluate.url_key(None) is None


def test_match_by_url_then_title_one_to_one() -> None:
    expected = [
        golden("Kế toán tổng hợp"),
        golden(url="https://acme.vn/jobs/2"),
        golden("Lập trình viên Python (Senior)"),
        golden("Nhân viên kho"),
    ]
    found = [
        predicted("Senior Python Developer", "https://acme.vn/jobs/2"),
        predicted("KẾ TOÁN TỔNG HỢP", "https://acme.vn/jobs/1"),
        predicted("Lập trình viên Python Senior", "https://acme.vn/jobs/3"),
        predicted("Lập trình viên Python - Senior", "https://acme.vn/jobs/4"),  # trùng, thừa
        predicted("Trưởng phòng kinh doanh", "https://acme.vn/jobs/5"),
    ]
    pairs = evaluate.match(expected, found)
    assert sorted(pairs) == [(0, 1), (1, 0), (2, 2)]  # điểm bằng nhau: lấy job đứng trước
    assert len(pairs) == 3  # mỗi job golden khớp tối đa một job


def test_title_threshold() -> None:
    assert evaluate.score(golden("Kế toán"), predicted("Kế toán viên")) >= 90
    assert evaluate.score(golden("Kế toán"), predicted("Kỹ sư xây dựng")) < 90
    assert evaluate.score(golden(url="https://x.vn/1"), predicted("A", "https://x.vn/2")) == 0


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_load_golden(tmp_path: Path) -> None:
    write(
        tmp_path / "companies.csv",
        "name,tax_code,website\nCông ty TNHH ACME,0101234567,acme.vn\nBeta JSC,,beta.vn\n",
    )
    write(
        tmp_path / "jobs.csv",
        "company,title,url\n"
        "ACME,Kế toán,\n"
        "0101234567,,https://acme.vn/jobs/2\n"
        "Beta,Lập trình viên,https://beta.vn/1\n"
        "Gamma,Bảo vệ,\n"
        "ACME,,\n",
    )
    g = evaluate.load_golden(tmp_path)
    assert [c.name for c in g.companies] == ["Công ty TNHH ACME", "Beta JSC"]
    assert [(j.title, j.url) for j in g.jobs[0]] == [
        ("Kế toán", None),
        (None, "https://acme.vn/jobs/2"),
    ]
    assert [j.title for j in g.jobs[1]] == ["Lập trình viên"]
    assert g.warnings == ["jobs.csv dòng 5: 'Gamma' không có ở companies.csv"]


def test_load_golden_errors(tmp_path: Path) -> None:
    with pytest.raises(GoldenError, match="thiếu"):
        evaluate.load_golden(tmp_path)
    write(tmp_path / "companies.csv", "name\nACME\n")
    write(tmp_path / "jobs.csv", "company,salary\nACME,10tr\n")
    with pytest.raises(GoldenError, match="cần cột"):
        evaluate.load_golden(tmp_path)


def test_report_numbers_and_markdown() -> None:
    report = EvalReport(
        generated_at=datetime(2026, 10, 17, tzinfo=UTC),
        companies=[
            CompanyEval("ACME", UUID(int=1), golden=10, predicted=10, matched=9, run_status="ok"),
            CompanyEval("Beta", None, golden=5, predicted=0, matched=0, missing=["Kế | toán"]),
        ],
        sources=[SourceEval("topcv", predicted=8, matched=8, golden=10)],
        warnings=[],
    )
    assert report.precision == 0.9
    assert report.recall == 0.6
    text = evaluate.markdown_report(report, perf={"p95": "120 s"})
    assert "Precision: **90.0%** (mục tiêu ≥ 95%: CHƯA ĐẠT)" in text
    assert "Recall: **60.0%**" in text
    assert "| ACME | ok | 10 | 10 | 9 | 90.0% | 90.0% |" in text
    assert "Kế / toán" in text  # không phá bảng markdown
    assert "| topcv | 8 | 8 | 100.0% | 80.0% |" in text
    assert "- p95: 120 s" in text
    data = evaluate.report_json(report)
    assert '"precision": 0.9' in data


def test_read_review(tmp_path: Path) -> None:
    path = tmp_path / "r.csv"
    write(path, "job_id,dung\n1,Y\n2,đúng\n3,sai\n4,?\n5,\n6,0\n")
    review = evaluate.read_review(path)
    assert (review.reviewed, review.correct, review.unknown) == (4, 2, 2)
    assert review.precision == 0.5
