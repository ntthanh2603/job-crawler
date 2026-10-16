"""Bóc danh sách vị trí từ trang tuyển dụng không có dữ liệu cấu trúc (fallback)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from jobcrawler.prompts.base import Prompt


class ListedJob(BaseModel):
    title: str = Field(description="Tên vị trí, chép nguyên văn từ trang")
    link: int | None = Field(
        default=None, description="Số thứ tự link tới trang chi tiết của vị trí, nếu có"
    )
    location: str | None = Field(default=None, description="Địa điểm ghi cạnh vị trí, nếu có")


class JobListing(BaseModel):
    is_job_listing: bool = Field(
        description="Trang có liệt kê các vị trí đang tuyển của doanh nghiệp hay không"
    )
    jobs: list[ListedJob] = Field(default_factory=list)
    listing_links: list[int] = Field(
        default_factory=list,
        description="Số thứ tự link tới trang danh sách vị trí khác (trang sau, xem tất cả), "
        "tối đa 3",
        max_length=3,
    )


PROMPT = Prompt(
    name="job_listing",
    version="2026-10-17.1",
    system=(
        "Bạn bóc danh sách vị trí tuyển dụng từ một trang web. Chỉ liệt kê vị trí thật sự có "
        "trên trang, chép đúng tên; không bịa vị trí hay link. Link chỉ được chọn theo số thứ "
        "tự trong danh sách cho sẵn. Bỏ qua tin tức, bài blog, mục menu. Chỉ trả về JSON đúng "
        "schema."
    ),
    user=(
        "URL trang: $url\n\n"
        "Nội dung trang (markdown):\n$page\n\n"
        "Các link trên trang (số thứ tự. chữ trên link — URL):\n$links"
    ),
)
