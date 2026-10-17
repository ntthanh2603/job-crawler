"""123job (123job.vn): sitemap rỗng.

- Danh sách: RSS `/rss/index` (100 tin mới nhất) + trang `/tuyen-dung?page=N` (30 tin/trang,
  đọc N trang đầu). Không bao giờ đọc đủ → không đánh dấu tin vắng mặt; hết hạn theo
  `validThrough`.
- URL tin `/viec-lam/<slug>-<mã 10 ký tự>`; `identifier` là mã số công ty;
  `employmentType` thường là "OTHER".
- Profile `/company/<slug>-<mã 10 ký tự>`: website ở mục "Website" (hay ghi "Đang cập nhật");
  trang danh sách tin của công ty không có link `/viec-lam/` → dùng tin đã index.
Mẫu: tests/fixtures/boards/123job/.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator

from jobcrawler.net.urls import normalize_url
from jobcrawler.pages import clean_text, parse
from jobcrawler.sources.base import SourceError, SourceSession
from jobcrawler.sources.boards.base import (
    BoardEntry,
    BoardJob,
    ListingBoard,
    ListingState,
    ProfileRef,
)

RSS = "https://123job.vn/rss/index"
_ITEM = re.compile(r"<item>(.*?)</item>", re.S | re.I)
_LINK = re.compile(r"<link>\s*(.*?)\s*</link>", re.S | re.I)


class Job123(ListingBoard):
    name = "123job"
    home = "https://123job.vn/"
    listing_url = "https://123job.vn/tuyen-dung?page={page}"
    max_listing_pages = 30
    listing_complete = False
    note = "RSS 100 tin + 30 trang danh sách; không đánh dấu tin gỡ"
    job_url = re.compile(r"^https://123job\.vn/viec-lam/[^/]+-(?P<id>[A-Za-z0-9]{10})$")
    profile_url = re.compile(r"^https://123job\.vn/company/[^/]+-(?P<id>[A-Za-z0-9]{10})$")
    profile_name_selector = ".content-name-header"
    profile_selector = '.company-name a[href*="/company/"]'
    profile_lists_jobs = False

    async def entries(
        self, session: SourceSession, state: ListingState
    ) -> AsyncIterator[BoardEntry]:
        seen: set[str] = set()
        result = await session.fetch(RSS, rate=self.rate, use_cache=False)
        if result.ok:
            for block in _ITEM.findall(result.text):
                if (m := _LINK.search(block)) is None:
                    continue
                url = self.canonical(m.group(1).replace("&amp;", "&"))
                if (external_id := self.job_id(url)) is not None and external_id not in seen:
                    seen.add(external_id)
                    yield BoardEntry(url=url, external_id=external_id)
        else:
            state.fail(f"RSS lỗi: {result.error}")
        try:
            async for entry in super().entries(session, state):
                if entry.external_id not in seen:
                    seen.add(entry.external_id)
                    yield entry
        except SourceError as exc:
            if not seen:
                raise
            state.fail(str(exc))  # đã có tin từ RSS

    def parse_job(self, html: str, url: str) -> BoardJob | None:
        parsed = super().parse_job(html, url)
        if parsed is None or parsed.job.employment_type != "OTHER":
            return parsed
        return BoardJob(
            job=parsed.job.model_copy(update={"employment_type": None}), profile=parsed.profile
        )

    def parse_profile(self, html: str, url: str) -> ProfileRef | None:
        profile = super().parse_profile(html, url)
        if profile is None:
            return None
        info = dict(profile.info)
        website = profile.website
        for item in parse(html).css(".introduce-item"):
            label_node = item.css_first(".item-text")
            label = clean_text(label_node.text()) if label_node is not None else ""
            value_node = item.css_first(".item-value")
            value = clean_text(value_node.text(deep=True, separator=" ")) if value_node else ""
            if not value or value.lower().startswith("đang cập nhật"):
                continue
            if label.lower().startswith("website") and (
                u := normalize_url(value if "://" in value else f"https://{value}")
            ):
                website = website or u
            elif label.lower().startswith("trụ sở"):
                info["address"] = value
            elif label.lower().startswith("quy mô"):
                info["size"] = value
        return profile.model_copy(update={"website": website, "info": info})
