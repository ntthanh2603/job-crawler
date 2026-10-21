"""Phân trang ở server cho danh sách của giao diện (như `GetManyBaseQueryParams` / `getManyResponse`
của open-asm): trang + số dòng mỗi trang, sắp xếp theo một cột được phép, tổng đếm có giới hạn
(`COUNT_CAP`)."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

DEFAULT_LIMIT = 10
MAX_LIMIT = 100
# Đếm tối đa chừng này dòng (COUNT trên bảng lớn có lọc rất tốn): quá thì hiện "10.000+" và chỉ
# cho đi tới trang cuối trong giới hạn — muốn xem tiếp thì lọc hẹp lại.
COUNT_CAP = 10_000


class SortOrder(StrEnum):
    ASC = "ASC"
    DESC = "DESC"


class GetManyQuery(BaseModel):
    """Tham số chung của mọi endpoint danh sách (như `GetManyBaseQueryParams` của open-asm): tìm
    kiếm, trang, số dòng mỗi trang, chiều sắp xếp. Mỗi endpoint kế thừa, khai `sortBy` (các cột
    được phép, `Literal`: giá trị khác bị từ chối 422) và bộ lọc riêng, rồi nhận bằng
    `Annotated[XxxQuery, Query()]`."""

    search: str = Field(default="", description="Tìm kiếm (cột tìm tuỳ endpoint)")
    page: int = Field(
        default=1, ge=1, description="Trang, từ 1 (quá trang cuối thì trả trang cuối)"
    )
    limit: int = Field(
        default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description=f"Số dòng mỗi trang (1–{MAX_LIMIT})"
    )
    sortOrder: SortOrder = Field(default=SortOrder.DESC, description="Chiều sắp xếp theo `sortBy`")


@dataclass(slots=True)
class Page[T]:
    items: list[T]
    total: int
    page: int
    limit: int = DEFAULT_LIMIT
    exact: bool = True  # False: có hơn `total` (= COUNT_CAP) dòng

    @property
    def page_count(self) -> int:
        return max(1, math.ceil(self.total / self.limit))

    @property
    def has_next_page(self) -> bool:
        return self.page * self.limit < self.total


def ordering(key: Any, order: SortOrder, *tiebreak: Any, nullable: bool = False) -> list[Any]:
    """ORDER BY theo `key` và chiều `order`, rồi các cột phá hoà cùng chiều (thứ tự ổn định giữa các
    trang). Cột có NULL (`nullable`) luôn để NULL cuối; cột NOT NULL thì không ghi NULLS để vẫn
    khớp index (Postgres không suy NULLS từ ràng buộc NOT NULL)."""
    asc = order is SortOrder.ASC
    first = key.asc() if asc else key.desc()
    if nullable:
        first = first.nulls_last()
    return [first, *(t.asc() if asc else t.desc() for t in tiebreak)]


async def capped_count(session: AsyncSession, stmt: Any, cap: int = COUNT_CAP) -> tuple[int, bool]:
    """Số dòng của `stmt`, đếm tối đa `cap` (+1 để biết có vượt không): (số, có chính xác không)."""
    limited = stmt.order_by(None).limit(cap + 1).subquery()
    n = int(await session.scalar(select(func.count()).select_from(limited)) or 0)
    return min(n, cap), n <= cap


async def paginate[T](
    session: AsyncSession,
    stmt: Any,
    page: int | None,
    *,
    order_by: Sequence[Any],
    limit: int = DEFAULT_LIMIT,
    scalars: bool = True,
    convert: Callable[[Any], T] | None = None,
) -> Page[T]:
    """Một trang của `stmt` (OFFSET trên index của `order_by`), tổng đếm có giới hạn. Trang quá
    trang cuối thì trả trang cuối."""
    limit = min(max(1, limit), MAX_LIMIT)
    total, exact = await capped_count(session, stmt)
    pages = max(1, math.ceil(total / limit))
    current = min(max(1, page or 1), pages)
    query = stmt.order_by(*order_by).limit(limit).offset((current - 1) * limit)
    rows: list[Any] = list(await (session.scalars(query) if scalars else session.execute(query)))
    items = [convert(r) for r in rows] if convert else rows
    return Page(items=items, total=total, page=current, limit=limit, exact=exact)
