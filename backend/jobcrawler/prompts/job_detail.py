"""Xác định một trang có phải tin tuyển dụng và bóc các trường ngắn (fallback).

Nội dung mô tả không lấy từ LLM (tránh bịa và tốn token): dùng markdown của trang.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from jobcrawler.prompts.base import Prompt


class JobDetail(BaseModel):
    is_job_posting: bool = Field(
        description="Trang là tin tuyển dụng cho đúng một vị trí (không phải danh sách, tin "
        "tức hay bài blog)"
    )
    title: str | None = Field(default=None, description="Tên vị trí, chép nguyên văn")
    company_name: str | None = Field(default=None, description="Tên nhà tuyển dụng ghi trên tin")
    locations: list[str] = Field(default_factory=list, description="Địa điểm làm việc")
    employment_type: str | None = Field(
        default=None, description="Loại hình: toàn thời gian, bán thời gian, thực tập..."
    )
    work_mode: str | None = Field(default=None, description="onsite, hybrid hoặc remote")
    level: str | None = Field(default=None, description="Cấp bậc ghi trên tin")
    salary: str | None = Field(default=None, description="Mức lương, chép nguyên văn")
    experience: str | None = Field(default=None, description="Yêu cầu kinh nghiệm, nguyên văn")
    deadline: str | None = Field(default=None, description="Hạn nộp hồ sơ, nguyên văn")
    posted: str | None = Field(default=None, description="Ngày đăng, nguyên văn")


PROMPT = Prompt(
    name="job_detail",
    version="2026-10-17.1",
    system=(
        "Bạn đọc một trang web và cho biết đó có phải tin tuyển dụng cho một vị trí hay không, "
        "rồi chép các trường được hỏi đúng như trên trang. Trường nào không có thì để null; "
        "không suy đoán. Chỉ trả về JSON đúng schema."
    ),
    user="URL: $url\nTiêu đề trang: $title\n\nNội dung trang (markdown):\n$page",
)
