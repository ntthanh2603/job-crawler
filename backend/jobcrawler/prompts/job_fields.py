"""Phân loại cấp bậc / loại hình / hình thức làm việc khi rule chưa quyết được.

Mỗi trường kèm trích đoạn nguyên văn làm bằng chứng; pipeline chỉ nhận giá trị có trích đoạn
thật sự nằm trong tin (`pipeline/normalize.py`), để LLM không đoán.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from jobcrawler.common.schemas import RawJob
from jobcrawler.prompts.base import Prompt

_EVIDENCE = "Trích nguyên văn (ngắn) đoạn trong tin cho thấy giá trị này; không có thì null"


class JobFields(BaseModel):
    level: Literal[
        "intern",
        "fresher",
        "junior",
        "middle",
        "senior",
        "lead",
        "manager",
        "director",
        "unknown",
    ] = Field(description="Cấp bậc của vị trí; tin không nêu thì unknown")
    level_evidence: str | None = Field(default=None, description=_EVIDENCE)
    employment_type: Literal[
        "full_time", "part_time", "contract", "internship", "freelance", "unknown"
    ] = Field(description="Loại hình làm việc; tin không nêu thì unknown")
    employment_type_evidence: str | None = Field(default=None, description=_EVIDENCE)
    work_mode: Literal["onsite", "hybrid", "remote", "unknown"] = Field(
        description="Làm tại văn phòng, kết hợp hay từ xa; tin không nêu rõ thì unknown"
    )
    work_mode_evidence: str | None = Field(default=None, description=_EVIDENCE)


def known_fields(raw: RawJob) -> str:
    """Các trường nguồn có ghi (giá trị thô) để LLM tham khảo."""
    pairs = [
        ("Cấp bậc", raw.level or raw.extra.get("occupational_category")),
        ("Loại hình", raw.employment_type),
        ("Hình thức", raw.work_mode),
        ("Kinh nghiệm", raw.experience_raw),
        ("Địa điểm", "; ".join(raw.locations[:3]) or None),
    ]
    return "\n".join(f"{k}: {v}" for k, v in pairs if v) or "(nguồn không ghi)"


PROMPT = Prompt(
    name="job_fields",
    version="2026-10-17.2",
    system=(
        "Bạn phân loại một tin tuyển dụng. Chỉ dựa vào nội dung được cho. Mỗi trường phải kèm "
        "trích đoạn nguyên văn trong tin làm bằng chứng; tin không nêu rõ thì trả unknown và "
        "bằng chứng null, không suy đoán (vd. không có chữ từ xa/remote/hybrid thì work_mode là "
        "unknown). Cấp bậc: intern (thực tập), fresher (mới tốt nghiệp), junior, middle, senior, "
        "lead (trưởng nhóm/giám sát), manager (trưởng/phó phòng, quản lý), director (giám đốc "
        "trở lên). Chỉ trả về JSON đúng schema."
    ),
    user=(
        "Tiêu đề: $title\nTrường nguồn ghi:\n$fields\n\nCần xác định: $unclear\n\n"
        "Nội dung tin (markdown):\n$jd"
    ),
)
