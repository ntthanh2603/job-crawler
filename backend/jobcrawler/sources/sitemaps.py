"""Đọc sitemap XML (`urlset` / `sitemapindex`, cả `.xml.gz`): dùng chung cho trang tuyển dụng
(`sources/boards/`) và trang careers của doanh nghiệp (`sources/careers.py`)."""

from __future__ import annotations

import gzip
import re
from datetime import datetime

from jobcrawler.sources.base import parse_datetime

_URL_BLOCK = re.compile(r"<url>(.*?)</url>", re.S | re.I)
_SITEMAP_BLOCK = re.compile(r"<sitemap>(.*?)</sitemap>", re.S | re.I)
_LOC = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?\s*(.*?)\s*(?:\]\]>)?\s*</loc>", re.S | re.I)
_LASTMOD = re.compile(r"<lastmod>\s*(.*?)\s*</lastmod>", re.S | re.I)


def sitemap_body(content: bytes) -> str | None:
    """Nội dung sitemap (giải nén `.xml.gz` nếu cần). None: file nén hỏng — sitemap lỗi chứ
    không phải sitemap rỗng (không thì danh sách thiếu mà vẫn được coi là đọc đủ)."""
    if content[:2] == b"\x1f\x8b":
        try:
            content = gzip.decompress(content)
        except (OSError, EOFError):
            return None
    return content.decode("utf-8", errors="replace")


def is_sitemap(text: str) -> bool:
    """Là sitemap XML thật (không phải trang HTML "không tìm thấy" trả mã 200)."""
    head = text[:2000].lower()
    return "<urlset" in head or "<sitemapindex" in head


def parse_sitemap(text: str) -> tuple[bool, list[tuple[str, datetime | None]]]:
    """(là sitemap index?, [(loc, lastmod)])."""
    is_index = "<sitemapindex" in text[:2000].lower()
    blocks = _SITEMAP_BLOCK.findall(text) if is_index else _URL_BLOCK.findall(text)
    found: list[tuple[str, datetime | None]] = []
    for block in blocks:
        if (loc := _LOC.search(block)) is None:
            continue
        lastmod = _LASTMOD.search(block)
        url = loc.group(1).replace("&amp;", "&").strip()
        found.append((url, parse_datetime(lastmod.group(1)) if lastmod else None))
    return is_index, found
