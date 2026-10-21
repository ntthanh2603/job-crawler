"""Fencing token cho việc ghi DB theo doanh nghiệp: chặn lượt build / poll đã mất khoá ghi đè.

Khoá Redis theo doanh nghiệp (`modules/crawl/crawl_service.company_lock`) chỉ loại trừ được khi mọi
process chạy đúng hạn: process đứng lâu hơn TTL khoá (GC, máy ngủ, event loop kẹt) rồi chạy tiếp vẫn
tưởng mình giữ khoá trong khi lượt khác đã lấy. Fencing token chặn ở chỗ ghi:

- Mỗi lần giữ khoá, `next_token()` tăng `company_fences.fence` của doanh nghiệp (transaction riêng)
  và trả số mới. Nguồn token là chính Postgres nên Redis mất dữ liệu không làm token lùi.
- Trong khối `fenced(company_id, token)` (contextvar: task con tạo trong khối thừa hưởng), mọi
  transaction của `GatedSession` chạy `SELECT … FOR SHARE` dòng `company_fences` với điều kiện
  `fence = token` ngay trước commit; không khớp (lượt mới đã lấy khoá) → `FencedOut`, rollback.
- `FOR SHARE` xung đột với `UPDATE` tăng token: lượt mới chỉ lấy được token sau khi các commit
  đang dở của lượt cũ xong, và từ đó mọi commit của lượt cũ bị từ chối. Transaction song song của
  cùng một lượt không chặn nhau.

Doanh nghiệp không có trong `companies` thì không có token (None) và không chặn gì. Ghi qua
`engine` trực tiếp (không qua session) không được kiểm.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Iterator
from contextvars import ContextVar

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

_current: ContextVar[tuple[uuid.UUID, int] | None] = ContextVar("company_fence", default=None)

_NEXT = text(
    """
    INSERT INTO company_fences (company_id, fence)
    SELECT id, 1 FROM companies WHERE id = :company_id
    ON CONFLICT (company_id) DO UPDATE SET fence = company_fences.fence + 1
    RETURNING fence
    """
)
_CHECK = text(
    "SELECT 1 FROM company_fences WHERE company_id = :company_id AND fence = :fence FOR SHARE"
)


class FencedOut(RuntimeError):
    """Lượt khác đã giữ khoá doanh nghiệp sau lượt này: lượt này không còn được ghi DB."""


async def next_token(
    sessionmaker: async_sessionmaker[AsyncSession], company_id: uuid.UUID
) -> int | None:
    """Tăng và trả fencing token của doanh nghiệp; None nếu không có doanh nghiệp này."""
    outer = _current.set(None)  # lần tăng không bị kiểm bởi token của khối ngoài (nếu có)
    try:
        async with sessionmaker.begin() as session:
            result = await session.execute(_NEXT, {"company_id": company_id})
            return result.scalar_one_or_none()
    finally:
        _current.reset(outer)


@contextlib.contextmanager
def fenced(company_id: uuid.UUID, token: int | None) -> Iterator[None]:
    """Mọi commit (`GatedSession`) trong khối kiểm token; `token` None thì không kiểm."""
    if token is None:
        yield
        return
    reset = _current.set((company_id, token))
    try:
        yield
    finally:
        _current.reset(reset)


def current() -> tuple[uuid.UUID, int] | None:
    """(doanh nghiệp, token) của khối `fenced` đang chạy."""
    return _current.get()


class GatedSession(Session):
    """Session kiểm fencing token (nếu context đang trong khối `fenced`) trước mỗi commit."""


@event.listens_for(GatedSession, "before_commit")
def _check_fence(session: Session) -> None:
    fence = _current.get()
    if fence is None:
        return
    company_id, token = fence
    row = session.execute(_CHECK, {"company_id": company_id, "fence": token}).first()
    if row is None:
        raise FencedOut(
            f"doanh nghiệp {company_id}: lượt khác đã giữ khoá (token {token} đã cũ), bỏ ghi"
        )
