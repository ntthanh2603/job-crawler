"""Danh bạ nhà tuyển dụng của một trang tuyển dụng (sitemap profile,
`BoardAdapter.company_sitemaps`).

Index chỉ biết nhà tuyển dụng có tin đã tải: doanh nghiệp chưa có tin nào trong index (lượt đủ có
giới hạn số tin, lượt đuôi chỉ thấy tin mới) thì không có profile để map, trước đây chỉ tìm được
bằng SERP. Danh bạ liệt kê mọi profile, tên nằm trong URL (`/cong-ty/<slug>/<id>.html`): profile
có khoá tên (`company_match_key` của slug) trùng khoá tên một doanh nghiệp được thêm vào
`source_profiles` (tên tạm lấy từ slug; lượt index gặp tin của profile thì ghi tên thật) rồi map
như mọi profile khác. Chỉ thêm profile khớp tên để bảng không phình theo cả trăm nghìn nhà tuyển
dụng không liên quan; doanh nghiệp nạp sau được xét ở lượt đủ kế tiếp.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from jobcrawler.common.text import company_match_key, distinctive_key
from jobcrawler.database.models import CompanyAlias, SourceProfile
from jobcrawler.sources.base import SourceSession
from jobcrawler.sources.boards.base import BoardAdapter
from jobcrawler.sources.sitemaps import is_sitemap, parse_sitemap, sitemap_body

MAX_SITEMAPS = 400  # TopCV ~250 file (10/2026)
_CHUNK = 1_000


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    external_id: str
    url: str
    name: str  # tên lấy từ slug: "cong ty co phan mat bao"


@dataclass(slots=True)
class Directory:
    entries: list[DirectoryEntry] = field(default_factory=list)
    sitemaps: int = 0
    errors: list[str] = field(default_factory=list)


def slug_name(slug: str) -> str:
    return " ".join(slug.replace("_", "-").split("-")).strip()


async def read_directory(
    adapter: BoardAdapter, session: SourceSession, *, max_sitemaps: int = MAX_SITEMAPS
) -> Directory:
    """Mọi profile trong danh bạ của trang (sitemap lỗi thì bỏ qua, ghi vào `errors`)."""
    out = Directory()
    found: dict[str, DirectoryEntry] = {}
    queue = list(adapter.company_sitemaps)
    seen: set[str] = set()
    while queue and out.sitemaps < max_sitemaps:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        out.sitemaps += 1
        result = await session.fetch(url, rate=adapter.rate, use_cache=False)
        body = sitemap_body(result.content) if result.ok else None
        if body is None or not is_sitemap(body):
            out.errors.append(url)
            continue
        is_index, items = parse_sitemap(body)
        if is_index:
            pattern = adapter.company_sitemap
            queue.extend(loc for loc, _ in items if pattern is None or pattern.search(loc))
            continue
        for loc, _ in items:
            canonical = adapter.canonical(loc)
            external_id = adapter.profile_id(canonical)
            slug = adapter.profile_slug(canonical)
            if external_id and slug and external_id not in adapter.placeholder_profiles:
                found.setdefault(
                    external_id, DirectoryEntry(external_id, canonical, slug_name(slug))
                )
    if queue:
        out.errors.append(f"vượt {max_sitemaps} sitemap")
    out.entries = list(found.values())
    return out


async def company_keys(session: AsyncSession) -> set[str]:
    """Khoá tên (`company_match_key`) của mọi alias doanh nghiệp, chỉ khoá đủ riêng."""
    aliases = await session.scalars(select(CompanyAlias.alias).distinct())
    keys = {company_match_key(alias) for alias in aliases}
    return {k for k in keys if distinctive_key(k)}


def matching(entries: Collection[DirectoryEntry], keys: Collection[str]) -> list[DirectoryEntry]:
    """Profile có khoá tên trùng khoá tên một doanh nghiệp."""
    return [e for e in entries if company_match_key(e.name) in keys]


async def add_profiles(
    session: AsyncSession, source: str, entries: Collection[DirectoryEntry]
) -> int:
    """Thêm profile chưa có (chưa map); profile đã có giữ nguyên. Trả số profile mới."""
    added = 0
    rows = [
        {
            "source": source,
            "external_id": e.external_id,
            "url": e.url,
            "name_on_source": e.name,
            "name_norm": company_match_key(e.name),
        }
        for e in entries
    ]
    for i in range(0, len(rows), _CHUNK):
        stmt = (
            pg_insert(SourceProfile)
            .values(rows[i : i + _CHUNK])
            .on_conflict_do_nothing(
                index_elements=[SourceProfile.source, SourceProfile.external_id]
            )
            .returning(SourceProfile.id)
        )
        added += len((await session.execute(stmt)).all())
    return added
