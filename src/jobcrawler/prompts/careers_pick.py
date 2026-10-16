"""Chọn trang careers trong danh sách link ứng viên (chỉ được chọn trong danh sách)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from jobcrawler.prompts.base import Prompt


class CareersPick(BaseModel):
    choices: list[int] = Field(
        description="Số thứ tự các link dẫn tới trang tuyển dụng/danh sách việc làm của chính "
        "doanh nghiệp, tốt nhất trước, tối đa 3. Rỗng nếu không link nào phù hợp.",
        max_length=3,
    )
    reason: str = Field(description="Một câu ngắn giải thích lựa chọn")


PROMPT = Prompt(
    name="careers_pick",
    version="2026-10-17.1",
    system=(
        "Bạn giúp tìm trang tuyển dụng trên website doanh nghiệp. Chỉ chọn trong danh sách link "
        "được cho, theo số thứ tự. Ưu tiên trang liệt kê các vị trí đang tuyển của chính doanh "
        "nghiệp đó; bỏ qua tin tức, blog, bài viết về văn hoá công ty, trang của công ty khác "
        "và trang tuyển dụng bên thứ ba. Chỉ trả về JSON đúng schema."
    ),
    user=(
        "Website: $site\n\n"
        "Các link ứng viên (số thứ tự. chữ trên link — URL):\n$links\n\n"
        "Chọn tối đa 3 link dẫn tới trang tuyển dụng của doanh nghiệp."
    ),
)
