"""Bộ khung thu thập của một nguồn (`crawl_plans.spec`): ghi ở lượt build, đọc ở lượt poll.

Lượt build (ban đêm) đi đủ đường: tìm nguồn trên website, đọc mọi trang danh sách, mở trình duyệt
khi cần, bắt API danh sách tin của trang SPA, mở trang chi tiết. Nó ghi lại những gì cần để lấy
lại danh sách tin mà không phải dò lại: trang danh sách (kể cả trang sau, trang danh mục dẫn tới
tin), trang nào phải mở bằng trình duyệt, API nào trả danh sách tin (cách phân trang), link nào đã
mở mà không phải tin.

Lượt poll (ban ngày) chỉ đọc đúng các trang / API đó (API gọi thẳng bằng HTTP, không mở trình
duyệt), so tập tin với lượt trước và chỉ mở trang chi tiết của tin mới. Không đọc được trang chính
nữa, hoặc trang không còn tin nào trong khi bộ khung có tin, thì nguồn bị đánh `broken` và doanh
nghiệp được build lại.
"""

from __future__ import annotations

from math import ceil
from typing import Any, Literal

from pydantic import BaseModel, Field

SPEC_VERSION = 1
# Link không phải tin giữ tối đa chừng này (poll không mở lại).
MAX_IGNORED = 500
MAX_INDIRECT = 2000


class ApiSpec(BaseModel):
    """API danh sách tin của trang SPA (`json_jobs.Pager`)."""

    url: str
    method: str = "GET"
    body: Any = None
    # False: phản hồi không phân trang (gọi lại đúng request một lần).
    paged: bool = True
    key: str = ""  # tham số trang / offset, đúng như trong request
    kind: Literal["page", "offset"] = "page"
    current: int = 1  # trang / offset của phản hồi bắt được
    size: int = 0  # số tin mỗi trang
    total: int | None = None
    in_body: bool = False
    embedded: bool = False  # JSON nhúng trong HTML (`__NEXT_DATA__`...), không phải API
    listing: str | None = None  # trang danh sách đã gọi API này


class PlanSpec(BaseModel):
    """Bộ khung một nguồn. Trường rỗng = không dùng."""

    version: int = SPEC_VERSION
    # careers: trang danh sách đã đọc (theo thứ tự đọc, kể cả trang sau / "xem tất cả").
    listings: list[str] = Field(default_factory=list)
    # Trang danh mục / theo tỉnh dẫn tới tin (mở như trang danh sách khi poll).
    hubs: list[str] = Field(default_factory=list)
    # Trang danh sách phải mở bằng trình duyệt (HTML là khung SPA / tin nạp bằng XHR).
    rendered: list[str] = Field(default_factory=list)
    # Trang (đã mở bằng trình duyệt) mà mọi tin đều có trong các API bắt được: poll chỉ gọi API.
    api_only: list[str] = Field(default_factory=list)
    apis: list[ApiSpec] = Field(default_factory=list)
    # Danh sách chỉ đọc được bằng LLM (trang không có cấu trúc): poll hỏi lại LLM khi trang đổi
    # (câu trả lời cache theo nội dung).
    llm_listing: bool = False
    # Link đã mở ở lượt build mà không phải tin: poll không mở lại.
    ignored: list[str] = Field(default_factory=list)
    # Mã tin chỉ tới được qua trang tin khác ("vị trí khác"): poll không thấy lại nên không coi
    # là đã gỡ; lượt build đêm (đọc đủ) mới đóng.
    indirect: list[str] = Field(default_factory=list)
    # Danh mục mã → tên trang tải kèm ([loại, mã, tên], vd. ["city", "33", "Hà Nội"]): poll gọi
    # thẳng API nên không có các danh mục này, đọc nơi làm của tin mới theo đây.
    names: list[list[str]] = Field(default_factory=list)
    # ATS: bảng tin để dựng lại nguồn (`AtsRef` dạng dict: plan.py không import sources.ats để
    # tránh vòng import).
    ats: dict[str, Any] | None = None
    jobs: int = 0  # số tin lượt build
    links: int = 0  # số link tin thấy trên các trang danh sách
    # Số trang (danh sách, danh mục, trang API) một lượt poll được đọc ở nguồn này: phần của nguồn
    # trong PLAN_MAX_PAGES của doanh nghiệp (`services.plans.allocate_pages`). 0 = bộ khung ghi
    # trước khi có giới hạn này: poll dùng PLAN_MAX_PAGES.
    pages: int = 0

    def add(
        self, field: Literal["listings", "hubs", "rendered", "api_only", "ignored"], url: str
    ) -> None:
        values: list[str] = getattr(self, field)
        if url not in values and (field != "ignored" or len(values) < MAX_IGNORED):
            values.append(url)

    def add_indirect(self, external_id: str) -> None:
        if external_id not in self.indirect and len(self.indirect) < MAX_INDIRECT:
            self.indirect.append(external_id)

    def add_api(self, api: ApiSpec) -> None:
        same = (api.url, api.method, api.key, api.paged)
        if all((a.url, a.method, a.key, a.paged) != same for a in self.apis):
            self.apis.append(api)

    def add_names(self, names: dict[tuple[str, str], str], *, limit: int = 2000) -> None:
        have = {(k, i) for k, i, _ in self.names}
        for (kind, item_id), name in names.items():
            if len(self.names) >= limit:
                return
            if (kind, item_id) not in have:
                self.names.append([kind, item_id, name])

    def name_map(self) -> dict[tuple[str, str], str]:
        return {(kind, item_id): name for kind, item_id, name in self.names}

    @property
    def empty(self) -> bool:
        return not (self.listings or self.apis or self.ats)

    def demand(self) -> int:
        """Số trang một lượt poll cần để đọc trọn nguồn: trang danh sách + danh mục (bỏ trang SPA
        mà API thay được) + các trang API (theo tổng số tin / cỡ trang bắt được ở lượt build).
        Bảng tin ATS tính một trang (Workday phân trang 20 tin)."""
        if self.ats is not None:
            per_page = 20 if self.ats.get("kind") == "workday" else 0
            return max(1, ceil(self.jobs / per_page)) if per_page else 1
        pages = len((set(self.listings) | set(self.hubs)) - set(self.api_only))
        for api in self.apis:
            total = ceil(api.total / api.size) if api.paged and api.total and api.size else 1
            pages += max(0, total - 1) if api.embedded else total
        return pages


def load_spec(data: dict[str, Any] | None) -> PlanSpec | None:
    """Bộ khung đã lưu; None nếu trống hoặc khác phiên bản (cần build lại)."""
    if not data or data.get("version") != SPEC_VERSION:
        return None
    return PlanSpec.model_validate(data)
