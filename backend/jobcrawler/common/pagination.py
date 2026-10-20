"""Phân trang ở server cho danh sách của giao diện, đếm có giới hạn (`COUNT_CAP`)."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

PAGE_SIZE = 50
# Đếm tối đa chừng này dòng (COUNT trên bảng lớn có lọc rất tốn): quá thì hiện "10.000+" và chỉ
# cho đi tới trang cuối trong giới hạn — muốn xem tiếp thì lọc hẹp lại.
COUNT_CAP = 10_000


@dataclass(slots=True)
class Page[T]:
    items: list[T]
    total: int
    page: int
    size: int = PAGE_SIZE
    exact: bool = True  # False: có hơn `total` (= COUNT_CAP) dòng

    @property
    def pages(self) -> int:
        return max(1, math.ceil(self.total / self.size))

    @property
    def first(self) -> int:
        return (self.page - 1) * self.size + 1 if self.total else 0

    @property
    def last(self) -> int:
        return min(self.total, self.page * self.size)


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
    size: int = PAGE_SIZE,
    scalars: bool = True,
    convert: Callable[[Any], T] | None = None,
) -> Page[T]:
    """Một trang của `stmt` (OFFSET trên index của `order_by`), tổng đếm có giới hạn."""
    total, exact = await capped_count(session, stmt)
    pages = max(1, math.ceil(total / size))
    current = min(max(1, page or 1), pages)
    query = stmt.order_by(*order_by).limit(size).offset((current - 1) * size)
    rows: list[Any] = list(await (session.scalars(query) if scalars else session.execute(query)))
    items = [convert(r) for r in rows] if convert else rows
    return Page(items=items, total=total, page=current, size=size, exact=exact)
