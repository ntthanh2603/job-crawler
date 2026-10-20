"""Nhận diện trang chặn bot, CAPTCHA, trang bắt đăng nhập.

Gặp các trang này thì dừng nguồn và ghi `blocked`; không tìm cách vượt.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import urlsplit

# Dấu hiệu chắc chắn của trang thử thách/chặn từ WAF, bất kể mã HTTP.
_WAF_MARKERS: tuple[tuple[str, str], ...] = (
    ("cdn-cgi/challenge-platform", "cloudflare_challenge"),
    ("cf-browser-verification", "cloudflare_challenge"),
    ("captcha-delivery.com", "datadome"),
    ("_incapsula_resource", "incapsula"),
    ("incapsula incident id", "incapsula"),
    ("px-captcha", "perimeterx"),
    ("/_sec/cp_challenge", "akamai"),
    ("sucuri website firewall", "sucuri"),
)

# Widget CAPTCHA: chỉ coi là chặn khi đi kèm mã lỗi hoặc trang rất ngắn, vì trang careers
# bình thường vẫn hay nhúng reCAPTCHA cho form ứng tuyển.
_CAPTCHA_MARKERS = (
    "g-recaptcha",
    "recaptcha/api.js",
    "h-captcha",
    "hcaptcha.com/1/api.js",
    "challenges.cloudflare.com/turnstile",
    "xác minh bạn không phải là robot",
    "bạn không phải là robot",
    "verify you are human",
    "are you a robot",
)
_SHORT_PAGE_BYTES = 15_000
# Trang chắn toàn trang (không phải widget trong form ứng tuyển): chặn bất kể độ dài/mã HTTP.
# CareerLink trả trang 200, ~27 KB "Xác nhận bạn không phải robot" với form /recaptcha.
_CAPTCHA_PAGE_MARKERS = (
    "recaptcha_confirm_form",
    'action="/recaptcha"',
    "hãy xác nhận bạn không phải robot",
    "chúng tôi nhận thấy có điều bất thường",
)

_CF_PASSIVE = re.compile(r"cdn-cgi/challenge-platform/(?:h/\w+/)?scripts/jsd/")

_LOGIN_PATH = re.compile(
    r"/(log-?in|sign-?in|dang-?nhap|auth|account/login|users?/login|sso)(/|$|\?)", re.IGNORECASE
)


def _is_login_redirect(requested_url: str, final_url: str) -> bool:
    if not final_url or final_url == requested_url:
        return False
    requested = urlsplit(requested_url)
    final = urlsplit(final_url)
    final_path = final.path + (f"?{final.query}" if final.query else "")
    req_path = requested.path + (f"?{requested.query}" if requested.query else "")
    return bool(_LOGIN_PATH.search(final_path)) and not _LOGIN_PATH.search(req_path)


def detect_block(
    status: int | None,
    headers: Mapping[str, str],
    body: str,
    *,
    requested_url: str,
    final_url: str,
) -> str | None:
    """Trả về mã lý do nếu là trang chặn/CAPTCHA/đăng nhập, ngược lại None.

    `headers` phải có khoá viết thường.
    """
    if headers.get("cf-mitigated", "").lower() == "challenge":
        return "cloudflare_challenge"

    # Script phát hiện bot thụ động của Cloudflare có trên trang bình thường (JobsGO...), không
    # phải trang thử thách: bỏ khỏi mẫu trước khi tìm dấu hiệu.
    sample = _CF_PASSIVE.sub("", body[:50_000].lower())
    if status in (403, 429, 503) and ("just a moment" in sample or "cf-chl" in sample):
        return "cloudflare_challenge"
    for marker, reason in _WAF_MARKERS:
        if marker in sample:
            return reason

    if status == 401:
        return "login_required"
    if _is_login_redirect(requested_url, final_url):
        return "login_required"

    if any(m in sample for m in _CAPTCHA_PAGE_MARKERS):
        return "captcha"
    if any(m in sample for m in _CAPTCHA_MARKERS) and (
        status in (403, 429, 503) or len(body) < _SHORT_PAGE_BYTES
    ):
        return "captcha"

    if status == 403:
        return "http_403"
    return None
