"""Chọn doanh nghiệp (trong danh sách cho sẵn) ứng với một profile nhà tuyển dụng trên trang
tuyển dụng. Chỉ dùng khi rule (MST, website, tên khớp) chưa quyết được."""

from __future__ import annotations

from pydantic import BaseModel, Field

from jobcrawler.prompts.base import Prompt


class ProfileMatch(BaseModel):
    company: str | None = Field(
        description="Mã doanh nghiệp (vd. C2) là đúng nhà tuyển dụng này; null nếu không chắc "
        "hoặc không doanh nghiệp nào khớp"
    )
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(description="Một câu, dựa trên bằng chứng trong dữ liệu được cho")


PROMPT = Prompt(
    name="profile_match",
    version="2026-10-17.1",
    system=(
        "Bạn đối chiếu nhà tuyển dụng trên một trang tuyển dụng với danh sách doanh nghiệp. Chỉ "
        "chọn khi chắc chắn là cùng một pháp nhân/thương hiệu (tên, website, mã số thuế, địa chỉ "
        'khớp). Tên gần giống nhưng khác ngành, khác địa phương, khác số ("số 1" và "số 2"), '
        "hoặc là công ty con/chi nhánh khác tên thì không chọn. Không chắc thì trả company=null. "
        "Chỉ trả về JSON đúng schema."
    ),
    user="Nhà tuyển dụng trên $board:\n$profile\n\nCác doanh nghiệp ứng viên:\n$companies",
)
