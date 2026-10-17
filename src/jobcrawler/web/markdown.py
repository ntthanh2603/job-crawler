"""Markdown tin tuyển dụng → HTML an toàn để giao diện hiển thị."""

from __future__ import annotations

from markdown_it import MarkdownIt

# Markdown do crawler sinh từ trang ngoài: không cho HTML thô (markdown-it escape), link mở tab mới.
_MD = MarkdownIt("commonmark", {"html": False, "linkify": False, "breaks": True}).enable("table")


def to_html(text: str | None) -> str:
    if not text:
        return ""
    return _MD.render(text).replace("<a href=", '<a target="_blank" rel="noopener" href=')
