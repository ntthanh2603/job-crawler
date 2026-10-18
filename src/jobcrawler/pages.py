"""Đọc trang HTML: link, tiêu đề, nội dung chính, markdown (selectolax + markdownify)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit

from markdownify import markdownify
from selectolax.lexbor import LexborHTMLParser, LexborNode

from jobcrawler.net.urls import normalize_url, same_site

# Phần không phải nội dung: menu, footer, form, script... Bỏ trước khi lấy nội dung chính.
_NOISE = (
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "iframe",
    "form",
    "button",
    "select",
    "nav",
    "footer",
    "aside",
    "body > header",
    "[role=navigation]",
    "[role=banner]",
    "[role=contentinfo]",
    "[aria-hidden=true]",
)
_MAIN = ("main", "[role=main]", "article", "#main", "#content", ".content", ".main-content")
# Khung của app SPA: HTML gần như rỗng, nội dung do JavaScript dựng.
_SPA_MARKERS = (
    'id="root"',
    'id="app"',
    'id="__next"',
    'id="__nuxt"',
    "ng-version",
    "data-reactroot",
    "window.__NUXT__",
    "__NEXT_DATA__",
)
_BLANK_LINES = re.compile(r"\n{3,}")
_TRAILING_SPACE = re.compile(r"[ \t]+\n")
_WS = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class Link:
    url: str  # tuyệt đối, đã chuẩn hoá
    text: str


def parse(html: str) -> LexborHTMLParser:
    return LexborHTMLParser(html)


def clean_text(text: str | None) -> str:
    return _WS.sub(" ", text or "").strip()


def node_text(node: LexborNode | None) -> str:
    return clean_text(node.text(deep=True, separator=" ")) if node is not None else ""


def base_url(tree: LexborHTMLParser, page_url: str) -> str:
    base = tree.css_first("base[href]")
    href = base.attributes.get("href") if base is not None else None
    if not href:
        return page_url
    return normalize_url(href, page_url) or page_url


def extract_links(tree: LexborHTMLParser, page_url: str) -> list[Link]:
    """Mọi `<a href>` http(s), theo thứ tự trên trang; text lấy từ nội dung, `title` hoặc
    `aria-label`."""
    base = base_url(tree, page_url)
    links: list[Link] = []
    for a in tree.css("a[href]"):
        href = (a.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            continue
        url = normalize_url(href, base)
        if url is None:
            continue
        text = node_text(a) or clean_text(
            a.attributes.get("title") or a.attributes.get("aria-label")
        )
        links.append(Link(url=url, text=text))
    return links


# Tham số / đường dẫn phân trang: ?page=2, ?p=2, ?trang=2, /page/2, /trang-2.
PAGE_PARAMS = frozenset({"page", "p", "trang", "paged", "pg", "pagenumber", "page_no"})
PAGE_PATH = re.compile(r"/(?:page|trang)[/-](\d+)/?$", re.IGNORECASE)


def page_number(url: str) -> int | None:
    """Số trang ghi trong URL (None: URL không có tham số / đường dẫn phân trang)."""
    for key, value in parse_qsl(urlsplit(url).query):
        if key.lower() in PAGE_PARAMS and value.isdigit():
            return int(value)
    if m := PAGE_PATH.search(urlsplit(url).path):
        return int(m.group(1))
    return None


def page_path(url: str) -> str:
    """Đường dẫn bỏ phần số trang (/tuyen-dung/page/2 → /tuyen-dung)."""
    return PAGE_PATH.sub("/", urlsplit(url).path).rstrip("/")


def next_page_urls(links: Iterable[Link], page_url: str) -> list[str]:
    """Các trang sau của cùng danh sách (cùng đường dẫn, số trang lớn hơn), theo thứ tự số."""
    current = page_number(page_url) or 1
    base = page_path(page_url)
    pages: dict[int, str] = {}
    for link in links:
        if not same_site(link.url, page_url) or link.url == page_url:
            continue
        number = page_number(link.url)
        if number is not None and number > current and page_path(link.url) == base:
            pages.setdefault(number, link.url)
    return [pages[n] for n in sorted(pages)]


def meta_content(tree: LexborHTMLParser, *names: str) -> str | None:
    for name in names:
        node = tree.css_first(f'meta[property="{name}"]') or tree.css_first(f'meta[name="{name}"]')
        if node is not None and (value := clean_text(node.attributes.get("content"))):
            return value
    return None


def page_title(tree: LexborHTMLParser) -> str | None:
    """`<h1>` đầu tiên, rồi `og:title`, rồi `<title>`."""
    for candidate in (
        node_text(tree.css_first("h1")),
        meta_content(tree, "og:title", "twitter:title"),
        node_text(tree.css_first("title")),
    ):
        if candidate:
            return candidate
    return None


def main_node(tree: LexborHTMLParser) -> LexborNode | None:
    """Vùng nội dung chính (đã bỏ menu, footer, script...): ứng viên dài nhất, mặc định body.

    Sửa trực tiếp `tree`; gọi trên một cây riêng nếu còn cần HTML gốc.
    """
    for selector in _NOISE:
        for node in tree.css(selector):
            node.decompose()
    best: LexborNode | None = None
    best_len = 0
    for selector in _MAIN:
        for node in tree.css(selector):
            length = len(node_text(node))
            if length > best_len:
                best, best_len = node, length
    body = tree.body
    # Vùng "chính" quá ngắn so với body thường là một khối con lạc đề (vd. thẻ article tin tức).
    if body is not None and (best is None or best_len < 0.3 * len(node_text(body))):
        return body
    return best


def html_to_markdown(html: str) -> str:
    """HTML (đoạn hoặc cả trang) → markdown gọn: bỏ ảnh, giữ tiêu đề, danh sách, link, bảng."""
    md = markdownify(
        html,
        heading_style="ATX",
        bullets="-",
        strip=["img", "picture", "video", "audio", "source"],
        escape_asterisks=False,
        escape_underscores=False,
    )
    md = _TRAILING_SPACE.sub("\n", md.replace("\xa0", " "))
    return _BLANK_LINES.sub("\n\n", md).strip()


def main_markdown(html: str) -> str:
    """Nội dung chính của một trang dưới dạng markdown."""
    node = main_node(parse(html))
    return html_to_markdown(node.html or "") if node is not None else ""


def visible_text_length(tree: LexborHTMLParser) -> int:
    body = tree.body
    if body is None:
        return 0
    clone = parse(body.html or "")
    for selector in ("script", "style", "noscript", "template", "svg"):
        for node in clone.css(selector):
            node.decompose()
    return len(node_text(clone.body))


def looks_like_spa_shell(html: str, tree: LexborHTMLParser | None = None) -> bool:
    """Trang do JavaScript dựng (HTML gần rỗng): cần mở bằng trình duyệt mới thấy nội dung."""
    tree = tree or parse(html)
    text_len = visible_text_length(tree)
    if text_len < 200:
        return True
    return text_len < 1500 and any(marker in html for marker in _SPA_MARKERS)
