"""Chấm một tin còn nghi ngờ sau khi qua các rule xác minh (`pipeline/verify.py`)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from jobcrawler.prompts.base import PRIORITY_VERIFY, Prompt


class JobVerification(BaseModel):
    is_job_posting: bool = Field(
        description="Là tin tuyển dụng cho một vị trí cụ thể (không phải tin tức, blog, danh mục)"
    )
    belongs_to_company: bool = Field(
        description="Nhà tuyển dụng thật sự là doanh nghiệp được hỏi (hoặc công ty con/thương "
        "hiệu của chính doanh nghiệp đó)"
    )
    is_agency: bool = Field(
        description="Headhunter/công ty cung ứng nhân sự đăng tuyển hộ cho khách hàng khác"
    )
    scam_risk: float = Field(
        ge=0,
        le=1,
        description="Nguy cơ lừa đảo: thu phí, đặt cọc, đa cấp, việc nhẹ lương cao, CTV online",
    )
    confidence: float = Field(ge=0, le=1, description="Độ chắc chắn của các nhận định trên")
    reasons: list[str] = Field(
        default_factory=list, description="Tối đa 3 lý do ngắn, dựa trên nội dung được cho"
    )


PROMPT = Prompt(
    name="job_verify",
    version="2026-10-17.1",
    system=(
        "Bạn kiểm tra một tin tuyển dụng thu thập tự động có đúng là tin của doanh nghiệp được "
        "hỏi hay không. Ưu tiên chính xác: không chắc thì belongs_to_company=false hoặc "
        "confidence thấp. Tin của công ty khác tên, tin headhunter/agency tuyển cho 'đối tác', "
        "'khách hàng', tin giấu tên công ty đều không thuộc doanh nghiệp. Chỉ trả về JSON đúng "
        "schema."
    ),
    user=(
        "Doanh nghiệp:\n$company\n\nTin tuyển dụng (nguồn $source):\n$job\n\n"
        "Điểm cần xem lại: $concerns\n\nNội dung tin (markdown):\n$jd"
    ),
    priority=PRIORITY_VERIFY,
)
