"""Tiện ích URL: chuẩn hoá, domain đăng ký (eTLD+1), so sánh cùng site."""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

# Hậu tố hai cấp hay gặp. Không cần đủ như Public Suffix List: chỉ dùng để biết hai URL có
# cùng một website doanh nghiệp hay không.
_MULTI_PART_SUFFIXES = frozenset(
    {
        # Việt Nam
        "com.vn",
        "net.vn",
        "org.vn",
        "edu.vn",
        "gov.vn",
        "ac.vn",
        "info.vn",
        "biz.vn",
        "name.vn",
        "pro.vn",
        "health.vn",
        "int.vn",
        "io.vn",
        # Khu vực / đối tác hay gặp
        "com.sg",
        "edu.sg",
        "com.my",
        "co.th",
        "co.id",
        "com.ph",
        "com.hk",
        "com.tw",
        "com.cn",
        "co.jp",
        "co.kr",
        "co.in",
        "com.au",
        "net.au",
        "org.au",
        "co.nz",
        "co.uk",
        "org.uk",
        "ac.uk",
        "com.br",
    }
)

_DEFAULT_PORTS = {"http": 80, "https": 443}
_TRACKING_PARAMS = ("utm_", "fbclid", "gclid", "mc_", "_hs", "ref_src")
# Các hàm thuần dưới đây được gọi hàng trăm nghìn lần mỗi phút khi chấm link trên trang (một lượt
# poll đọc vài trang, mỗi trang vài trăm link): nhớ kết quả thay vì tách URL lại.
_CACHE = 1 << 16


@lru_cache(maxsize=_CACHE)
def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def strip_www(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


@lru_cache(maxsize=_CACHE)
def registrable_domain(host_or_url: str) -> str:
    """`careers.fpt.com.vn` → `fpt.com.vn`; `https://www.vng.com.vn/x` → `vng.com.vn`."""
    host = host_of(host_or_url) if "/" in host_or_url else host_or_url.lower().strip(".")
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if ".".join(labels[-2:]) in _MULTI_PART_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def same_site(a: str, b: str) -> bool:
    """Cùng domain đăng ký (subdomain như careers./tuyendung. vẫn tính là cùng site)."""
    ha, hb = (host_of(x) if "/" in x else x.lower() for x in (a, b))
    return bool(ha) and registrable_domain(ha) == registrable_domain(hb)


def parse_domain_input(value: str) -> str:
    """Domain từ input người dùng: `https://www.Base.vn/careers` → `base.vn`."""
    value = value.strip()
    if "://" not in value:
        value = "https://" + value
    return strip_www(host_of(value))


@lru_cache(maxsize=_CACHE)
def normalize_url(url: str, base: str | None = None) -> str | None:
    """URL tuyệt đối, bỏ fragment, cổng mặc định, tham số tracking. None nếu không phải http(s)."""
    url = url.strip()
    if base is not None:
        url = urljoin(base, url)
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    netloc = host if port in (None, _DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    query = urlencode(
        [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith(_TRACKING_PARAMS)
        ],
        doseq=True,
    )
    return urlunsplit((scheme, netloc, parts.path or "/", query, ""))


def path_segments(url: str) -> list[str]:
    return list(_path_segments(url))


@lru_cache(maxsize=_CACHE)
def _path_segments(url: str) -> tuple[str, ...]:
    return tuple(s for s in urlsplit(url).path.split("/") if s)


def site_root(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/"
