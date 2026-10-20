"""Thông tin định danh doanh nghiệp đọc từ trang web và kết quả tìm kiếm (dùng cho KYC, map
profile trang tuyển dụng, xác minh nhà tuyển dụng).

- MST có nhãn ("MST:", "Mã số thuế", "GPĐKKD", "Giấy CNĐKDN số"...), số trần bị bỏ qua.
- So tên doanh nghiệp (bỏ dấu, bỏ loại hình pháp lý), so domain với tên.
- Chân trang, JSON-LD Organization, tên trang tự ghi.
- Ẩn email, số điện thoại, tên người đại diện trong trích đoạn được lưu.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from rapidfuzz import fuzz
from selectolax.lexbor import LexborHTMLParser, LexborNode

from jobcrawler.common.pages import Link, meta_content, node_text, parse
from jobcrawler.common.text import fold, normalize_company_name, strip_accents
from jobcrawler.common.urls import registrable_domain, same_site
from jobcrawler.sources import jsonld

Kind = Literal["website", "registry", "other"]
HIDDEN = "[liên hệ đã ẩn]"
FOOTER_CHARS = 1500
SNIPPET_CHARS = 300


# Trang tra cứu MST / thông tin doanh nghiệp công khai.
REGISTRY_DOMAINS = frozenset(
    {
        "masothue.com",
        "masothue.vn",
        "thongtindoanhnghiep.co",
        "thongtincongty.co",
        "hosocongty.vn",
        "hosodoanhnghiep.vn",
        "infodoanhnghiep.com",
        "doanhnghiep.biz",
        "doanhnghiep.me",
        "doanhnghiepmoi.vn",
        "tratencongty.com",
        "masocongty.vn",
        "mst.vn",
        "hsctvn.com",
        "vinabiz.org",
        "thuvienphapluat.vn",
        "trangvangvietnam.com",
        "yellowpages.vn",
        "dangkykinhdoanh.gov.vn",
        "tracuunnt.gdt.gov.vn",
        "vietnamcredit.com.vn",
        "dnb.com",
        "opencorporates.com",
        "emis.com",
    }
)
# Không phải website của doanh nghiệp: mạng xã hội, trang tuyển dụng, báo, bách khoa, sàn TMĐT,
# kho ứng dụng, dữ liệu doanh nghiệp. Trừ khi domain mang đúng tên đầu vào (vd. "Tiki" → tiki.vn).
NON_COMPANY_DOMAINS = frozenset(
    {
        "facebook.com",
        "fb.com",
        "linkedin.com",
        "youtube.com",
        "instagram.com",
        "tiktok.com",
        "twitter.com",
        "x.com",
        "zalo.me",
        "pinterest.com",
        "threads.net",
        "wikipedia.org",
        "wikiwand.com",
        "google.com",
        "apple.com",
        "topcv.vn",
        "vietnamworks.com",
        "careerviet.vn",
        "careerbuilder.vn",
        "itviec.com",
        "topdev.vn",
        "jobsgo.vn",
        "careerlink.vn",
        "glints.com",
        "vieclam24h.vn",
        "123job.vn",
        "timviec365.vn",
        "joboko.com",
        "mywork.com.vn",
        "jobstreet.com",
        "jobstreet.vn",
        "indeed.com",
        "glassdoor.com",
        "ybox.vn",
        "vnexpress.net",
        "cafef.vn",
        "cafebiz.vn",
        "vietstock.vn",
        "tuoitre.vn",
        "thanhnien.vn",
        "dantri.com.vn",
        "znews.vn",
        "zingnews.vn",
        "vietnamnet.vn",
        "baomoi.com",
        "vneconomy.vn",
        "ndh.vn",
        "tinnhanhchungkhoan.vn",
        "vtv.vn",
        "nld.com.vn",
        "laodong.vn",
        "vov.vn",
        "vietnamplus.vn",
        "theleader.vn",
        "forbes.vn",
        "kenh14.vn",
        "genk.vn",
        "24h.com.vn",
        "nhandan.vn",
        "baodautu.vn",
        "vir.com.vn",
        "tienphong.vn",
        "plo.vn",
        "vietnambiz.vn",
        "nguoiquansat.vn",
        "brandsvietnam.com",
        "cophieu68.vn",
        "simplize.vn",
        "fireant.vn",
        "stockbiz.vn",
        "bloomberg.com",
        "reuters.com",
        "crunchbase.com",
        "zoominfo.com",
        "rocketreach.co",
        "apollo.io",
        "shopee.vn",
        "lazada.vn",
        "tiki.vn",
        "sendo.vn",
    }
)
_GENERIC_LABELS = frozenset(
    {"vietnam", "congty", "company", "group", "global", "online", "home", "shop", "store"}
)
_LABEL_SUFFIXES = ("vietnam", "group", "corp", "vn", "jsc")

_TAX_LABEL = (
    r"(?:ma so thue|ma so doanh nghiep|ma doanh nghiep|ma so dn|\bmst\b|\bmsdn\b|"
    r"tax (?:identification )?(?:code|id|number|no)|\bvat (?:code|id|number|no)|"
    r"business (?:registration|code|license|id)(?: (?:number|no|code|certificate))?|"
    r"enterprise (?:code|number)|"
    r"(?:giay )?(?:chung nhan|cn) ?(?:dang ky|dk) ?(?:doanh nghiep|dn|kinh doanh|kd)|"
    r"\bg?cn ?dk ?(?:dn|kd)\b|\bgp ?(?:dk)?kd\b|giay phep (?:kinh doanh|dkkd)|\bdkkd\b|"
    r"\bdkdn\b)"
)
# Dòng chân trang mang thông tin định danh (giữ lại khi chân trang dài).
_IDENTITY_LINE = re.compile(
    r"\b(dia chi|tru so|head office|address|copyright|ban quyen|giay chung nhan|giay phep|"
    r"ma so|mst|tax|dang ky kinh doanh|gpdkkd|dkkd|cong ty|tong cong ty|tap doan|ngan hang|"
    r"joint stock|co ltd|corporation|jsc|company limited)\b"
)
_SENTENCE_END = re.compile(r"(?<=[.;|])\s+")
_TAX_LABELED = re.compile(_TAX_LABEL + r"[^0-9\n]{0,40}?(\d{10}(?:\s?[-–]\s?\d{3})?)(?!\d)")
_TAX_BARE = re.compile(r"^\s*(\d{10}(?:\s?[-–]\s?\d{3})?)(?!\d)")
_TAX_IN_URL = re.compile(r"/(\d{10}(?:-\d{3})?)(?=[-/.]|$)")
_TAX_FORMAT = re.compile(r"^\d{10}(?:-\d{3})?$")
_LEGAL_LINE = re.compile(
    r"\b(cong ty|tong cong ty|tap doan|ngan hang|joint stock|company|co ltd|corporation|"
    r"corp|jsc|limited|ltd)\b"
)
_COPYRIGHT = re.compile(
    r"(?i)(©|\(c\)|copyright|bản quyền( thuộc về)?|all rights reserved|®|\b(19|20)\d{2}\b)"
)
_PARENS = re.compile(r"\(([^()]{2,60})\)")
_TITLE_SPLIT = re.compile(r"\s+[|\-–—:]\s+")
_CONTACT_TEXT = re.compile(r"\b(lien he|contact|ve chung toi|gioi thieu|about)\b")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_LABELED = re.compile(
    r"(?i)(\b(?:tel|điện thoại|dien thoai|hotline|phone|sđt|sdt|đt|dt|fax|mobile|di động|zalo)"
    r"\s*[:：.]?\s*)(\+?\d[\d\s.()-]{7,16}\d)"
)
_PHONE_GROUPED = re.compile(r"(?:\+84|(?<!\d)0)\d{1,3}[\s.]\d{3,4}[\s.]\d{3,4}(?!\d)")
_REPRESENTATIVE = re.compile(
    r"(?i)(người đại diện(?: pháp luật)?|đại diện pháp luật|legal representative)"
    r"\s*[:：]?\s*[^,.;|\n]{2,60}"
)


# --- chuỗi -----------------------------------------------------------------------------------


def scrub(text: str) -> str:
    """Ẩn email, số điện thoại, tên người đại diện trong trích đoạn được lưu."""
    text = _EMAIL.sub(HIDDEN, text)
    text = _REPRESENTATIVE.sub(lambda m: f"{m.group(1)}: {HIDDEN}", text)
    text = _PHONE_LABELED.sub(lambda m: f"{m.group(1)}{HIDDEN}", text)
    return _PHONE_GROUPED.sub(HIDDEN, text)


def shorten(text: str, limit: int = SNIPPET_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _norm_tax(value: str) -> str:
    return re.sub(r"\s*[-–]\s*", "-", value.strip())


def tax_codes_in(text: str) -> list[str]:
    """MST/mã số doanh nghiệp có nhãn đi trước ("MST:", "Mã số thuế", "Tax code", "Giấy
    CNĐKDN số"...). Số 10 chữ số không nhãn bị bỏ qua vì trùng dạng số điện thoại."""
    plain = strip_accents(text).lower()
    return list(dict.fromkeys(_norm_tax(m.group(1)) for m in _TAX_LABELED.finditer(plain)))


def registry_tax_codes(title: str, url: str, snippet: str = "") -> list[str]:
    """Trang tra cứu thường đặt MST ở đầu tiêu đề ("0300588569 - CÔNG TY...") và trong URL."""
    found = tax_codes_in(f"{title}\n{snippet}")
    if m := _TAX_BARE.match(title):
        found.append(_norm_tax(m.group(1)))
    if m := _TAX_IN_URL.search(url):
        found.append(m.group(1))
    return list(dict.fromkeys(found))


def valid_tax_code(value: str | None) -> str | None:
    if not value:
        return None
    value = _norm_tax(re.sub(r"[^\d\-–\s]", "", value))
    return value if _TAX_FORMAT.match(value) else None


def name_variants(name: str) -> list[str]:
    """Tên + phần trong ngoặc + tên bỏ ngoặc: "Ngân hàng TMCP Ngoại thương Việt Nam
    (Vietcombank)" → [..., "Vietcombank", "Ngân hàng TMCP Ngoại thương Việt Nam"]."""
    variants = [name]
    inner = _PARENS.findall(name)
    variants += inner
    if inner:
        variants.append(_PARENS.sub(" ", name))
    return [v for v in dict.fromkeys(" ".join(x.split()) for x in variants) if v]


def names_match(a: str, b: str) -> bool:
    """Hai tên chỉ cùng một doanh nghiệp: giống nhau sau khi chuẩn hoá (bỏ dấu, bỏ loại hình
    pháp lý, bỏ khoảng trắng) hoặc gần như giống (ratio ≥ 90 với tên đủ dài)."""
    for va in name_variants(a):
        na = normalize_company_name(va)
        for vb in name_variants(b):
            nb = normalize_company_name(vb)
            if not na or not nb:
                continue
            if na == nb or na.replace(" ", "") == nb.replace(" ", ""):
                return True
            if min(len(na), len(nb)) >= 6 and fuzz.ratio(na, nb) >= 90:
                return True
    return False


def domain_label(domain: str) -> str:
    return registrable_domain(domain).split(".")[0].replace("-", "")


def domain_matches_name(domain: str, name: str) -> bool:
    """Domain mang đúng tên: vinamilk.com.vn ~ "Vinamilk", thegioididong.com ~ "Công ty CP Thế
    Giới Di Động", vietcombank.com.vn ~ "Ngân hàng ... (Vietcombank)"."""
    label = domain_label(domain)
    if len(label) < 3:
        return False
    labels = {label}
    for suffix in _LABEL_SUFFIXES:
        if label.endswith(suffix) and len(label) - len(suffix) >= 4:
            labels.add(label.removesuffix(suffix))
    for variant in name_variants(name):
        tokens = normalize_company_name(variant).split()
        if "".join(tokens) in labels:
            return True
        # Một đoạn liên tiếp của tên (đủ dài, không phải từ chung chung) trùng nhãn domain.
        for lab in labels:
            if len(lab) < 5 or lab in _GENERIC_LABELS:
                continue
            for i in range(len(tokens)):
                acc = ""
                for token in tokens[i:]:
                    acc += token
                    if acc == lab:
                        return True
                    if len(acc) >= len(lab):
                        break
    return False


def present(value: str, haystack: str, *, fuzzy: bool = True, threshold: float = 90) -> bool:
    """`value` có trong `haystack` (đã `fold`, nên không phân biệt hoa thường/dấu câu): khớp
    nguyên cụm từ; `fuzzy` thì nhận cả chuỗi dài gần đúng (trích đoạn, địa chỉ hay bị chép lệch).
    Tên phải khớp nguyên văn: LLM không được tự dịch hay đổi loại hình pháp lý."""
    needle = fold(value)
    if len(needle) < 2:
        return False
    if f" {needle} " in f" {haystack} ":
        return True
    return fuzzy and len(needle) >= 12 and fuzz.partial_ratio(needle, haystack) >= threshold


def classify(domain: str, name: str) -> Kind:
    if domain in REGISTRY_DOMAINS:
        return "registry"
    if domain in NON_COMPANY_DOMAINS or domain.endswith(".gov.vn"):
        return "website" if domain_matches_name(domain, name) else "other"
    return "website"


# --- đọc website ------------------------------------------------------------------------------


@dataclass(slots=True)
class SiteFacts:
    """Thông tin định danh đọc từ một trang của website ứng viên."""

    url: str
    title: str | None = None
    site_name: str | None = None
    org: dict[str, str] = field(default_factory=dict)  # JSON-LD Organization
    footer: str = ""
    tax_codes: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)  # tên pháp lý/thương hiệu trang tự ghi

    def text(self) -> str:
        parts = [self.title or "", self.site_name or "", *self.org.values(), self.footer]
        return "\n".join(p for p in parts if p)

    def describe(self, label: str) -> str:
        lines = [f"{label} {self.url}"]
        if self.title:
            lines.append(f"Tiêu đề: {self.title}")
        if self.site_name and self.site_name != self.title:
            lines.append(f"Tên site: {self.site_name}")
        if self.org:
            lines.append("JSON-LD: " + "; ".join(f"{k}={v}" for k, v in self.org.items()))
        if self.tax_codes:
            lines.append("MST ghi trên trang: " + ", ".join(self.tax_codes))
        if self.footer:
            lines.append("Chân trang:\n" + self.footer)
        return "\n".join(lines)


def _footer_nodes(tree: LexborHTMLParser) -> list[LexborNode]:
    nodes = tree.css("footer") or tree.css("[role=contentinfo]")
    if nodes:
        return nodes
    outer: list[LexborNode] = []
    seen: set[int] = set()
    for node in tree.css("[id*=footer], [class*=footer]"):
        parent = node.parent
        while parent is not None and parent.mem_id not in seen:
            parent = parent.parent
        if parent is None:  # không nằm trong khối footer đã lấy
            outer.append(node)
            seen.add(node.mem_id)
    return outer


def _lines(text: str) -> list[str]:
    return list(dict.fromkeys(" ".join(line.split()) for line in text.split("\n") if line.strip()))


def _clean_name_line(line: str) -> str:
    line = _COPYRIGHT.sub(" ", line)
    return " ".join(line.strip(" .,-–|:").split())


def _sentences(line: str) -> list[str]:
    """Tách câu: "© 2018. Công ty CP ABC. GPĐKKD: 0303..." → ["© 2018.", "Công ty CP ABC.", ...]."""
    out: list[str] = []
    for part in _SENTENCE_END.split(line):
        if out and not (part[:1].isupper() or part[:1].isdigit() or part[:1] == "©"):
            out[-1] += " " + part
        else:
            out.append(part)
    return out


def legal_name_lines(text: str) -> list[str]:
    """Câu ngắn có loại hình pháp lý: "Công ty cổ phần Base Enterprise", "© 2026 ABC JSC"."""
    found: list[str] = []
    for line in _lines(text):
        for sentence in _sentences(line):
            if len(sentence) > 160 or not _LEGAL_LINE.search(fold(sentence)):
                continue
            cleaned = _clean_name_line(sentence)
            # Có loại hình pháp lý kèm tên riêng (bỏ dòng menu chỉ có chữ "Công ty").
            if len(cleaned) >= 6 and normalize_company_name(cleaned) != fold(cleaned):
                found.append(cleaned)
    return list(dict.fromkeys(found))


def footer_excerpt(lines: list[str], limit: int = FOOTER_CHARS) -> str:
    """Rút gọn chân trang dài: giữ các dòng định danh (tên pháp lý, MST, địa chỉ, bản quyền),
    phần còn lại lấy từ cuối; giữ thứ tự gốc."""
    keep: set[int] = set()
    used = 0
    for i, line in enumerate(lines):
        if len(line) <= 400 and _IDENTITY_LINE.search(fold(line)) and used + len(line) <= limit:
            keep.add(i)
            used += len(line) + 1
    for i in range(len(lines) - 1, -1, -1):
        if i not in keep and used + len(lines[i]) <= limit:
            keep.add(i)
            used += len(lines[i]) + 1
        elif i not in keep:
            break
    return "\n".join(line for i, line in enumerate(lines) if i in keep)


def site_facts(html: str, url: str) -> SiteFacts:
    """Chân trang, JSON-LD Organization, MST có nhãn, tên trang tự ghi."""
    org: dict[str, str] = {}
    for node in jsonld.organizations(html, url):
        for key, value in jsonld.organization_facts(node).items():
            org.setdefault(key, value)
    tree = parse(html)
    title = node_text(tree.css_first("title")) or None
    site_name = meta_content(tree, "og:site_name")
    for selector in ("script", "style", "noscript", "template", "svg"):
        for node in tree.css(selector):
            node.decompose()
    body_text = tree.body.text(deep=True, separator="\n") if tree.body is not None else ""
    footer_text = "\n".join(n.text(deep=True, separator="\n") for n in _footer_nodes(tree))
    # Không có khối footer (hoặc footer dựng bằng JS): lấy phần cuối trang.
    footer = footer_excerpt(_lines(footer_text.strip() or body_text[-3 * FOOTER_CHARS :]))
    tax_codes = tax_codes_in(body_text)
    for key in ("taxID", "vatID"):
        if (code := valid_tax_code(org.get(key))) is not None and code not in tax_codes:
            tax_codes.append(code)
    names = [org[k] for k in ("legalName", "name") if k in org]
    names += [s.strip() for s in org.get("alternateName", "").split(",") if s.strip()]
    names += legal_name_lines(footer)
    if site_name:
        names.append(site_name)
    if title:
        names += [part for part in _TITLE_SPLIT.split(title) if part]
    return SiteFacts(
        url=url,
        title=title,
        site_name=site_name,
        org=org,
        footer=footer,
        tax_codes=tax_codes,
        names=list(dict.fromkeys(n for n in names if n)),
    )


def contact_link(links: list[Link], page_url: str) -> str | None:
    """Link trang liên hệ/giới thiệu cùng site (thường ghi tên pháp lý, MST, địa chỉ)."""
    for link in links:
        if not same_site(link.url, page_url) or link.url.rstrip("/") == page_url.rstrip("/"):
            continue
        if _CONTACT_TEXT.search(fold(link.text)) or _CONTACT_TEXT.search(
            fold(link.url.replace("-", " "))
        ):
            return link.url
    return None
