"""Tin thật từ fixture của M2–M4 (trang tuyển dụng, ATS, careers) cho test chuẩn hoá/xác minh."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jobcrawler.common.schemas import ExtractionMethod, RawJob
from jobcrawler.pipeline.normalize import NormalizedJob, normalize_job
from jobcrawler.sources import boards

FIXTURES = Path(__file__).parent / "fixtures"
# Ngày các fixture được lưu (tin còn hạn ở thời điểm này).
NOW = datetime(2026, 10, 17, 3, 0, tzinfo=UTC)


def board_job(board: str, name: str) -> RawJob:
    html = (FIXTURES / "boards" / board / name).read_text(encoding="utf-8")
    url = html.splitlines()[0].strip().removeprefix("<!--").removesuffix("-->").strip()
    parsed = boards.registry()[board].parse_job(html, url)
    assert parsed is not None, f"{board}/{name}"
    return parsed.job


def board_jobs() -> list[tuple[str, RawJob]]:
    out: list[tuple[str, RawJob]] = []
    for directory in sorted((FIXTURES / "boards").iterdir()):
        for path in sorted(directory.glob("job_*.html")):
            out.append((f"{directory.name}/{path.name}", board_job(directory.name, path.name)))
    return out


def load_json(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def normalized(raw: RawJob, *, board: bool | None = None) -> NormalizedJob:
    is_board = raw.source in boards.registry() if board is None else board
    return normalize_job(raw, now=NOW, board=is_board)


def raw_job(**fields: Any) -> RawJob:
    """RawJob dựng tay: mặc định một tin careers JSON-LD hợp lệ của ACME."""
    values: dict[str, Any] = {
        "source": "careers",
        "external_id": "acme-1",
        "url": "https://careers.acme.vn/jobs/ke-toan-tong-hop",
        "title": "Kế toán tổng hợp",
        "company_name": "Công ty Cổ phần ACME",
        "description": (
            "<h2>Mô tả công việc</h2><ul><li>Hạch toán, kiểm tra chứng từ kế toán hằng ngày."
            "</li><li>Lập báo cáo tài chính, báo cáo thuế định kỳ theo quy định.</li><li>Phối "
            "hợp kiểm toán độc lập trong kỳ kiểm toán năm.</li></ul><h2>Yêu cầu</h2><ul><li>"
            "Tốt nghiệp đại học chuyên ngành kế toán, kiểm toán.</li><li>Ít nhất 2 năm kinh "
            "nghiệm kế toán tổng hợp.</li></ul>"
        ),
        "locations": ["Tầng 5, 12 Duy Tân, Cầu Giấy, Hà Nội"],
        "salary_raw": "15 - 20 triệu",
        "posted_at": datetime(2026, 10, 10, tzinfo=UTC),
        "expires_at": datetime(2026, 11, 30, tzinfo=UTC),
        "method": ExtractionMethod.JSONLD,
    }
    values.update(fields)
    return RawJob(**values)
