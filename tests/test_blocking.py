from __future__ import annotations

import pytest

from jobcrawler.net.blocking import detect_block

URL = "https://careers.example.vn/jobs"
BIG_PAGE = "<html><body>" + "<p>Mô tả công việc</p>" * 2000 + "</body></html>"


def check(
    status: int | None,
    body: str = "",
    headers: dict[str, str] | None = None,
    final_url: str = URL,
) -> str | None:
    return detect_block(status, headers or {}, body, requested_url=URL, final_url=final_url)


def test_normal_page_is_not_blocked() -> None:
    assert check(200, BIG_PAGE) is None
    assert check(404, "not found") is None


def test_cloudflare_header() -> None:
    assert check(403, "", {"cf-mitigated": "challenge"}) == "cloudflare_challenge"


def test_cloudflare_challenge_body() -> None:
    body = "<title>Just a moment...</title><script src='/cdn-cgi/challenge-platform/x'></script>"
    assert check(503, body) == "cloudflare_challenge"


@pytest.mark.parametrize(
    ("marker", "reason"),
    [
        ("https://geo.captcha-delivery.com/captcha/", "datadome"),
        ("Incapsula incident ID: 123", "incapsula"),
        ('<div id="px-captcha"></div>', "perimeterx"),
    ],
)
def test_waf_markers(marker: str, reason: str) -> None:
    assert check(200, f"<html>{marker}</html>") == reason


def test_captcha_on_error_status() -> None:
    body = BIG_PAGE + '<div class="g-recaptcha"></div>'
    assert check(403, body) == "captcha"
    assert check(429, body) == "captcha"


def test_recaptcha_on_normal_big_page_is_fine() -> None:
    # Trang careers bình thường có reCAPTCHA cho form ứng tuyển: không phải trang chặn.
    assert check(200, BIG_PAGE + '<script src="https://www.google.com/recaptcha/api.js">') is None


def test_short_page_with_captcha_is_blocked() -> None:
    assert check(200, "<p>Vui lòng xác minh bạn không phải là robot</p>") == "captcha"


def test_login_required() -> None:
    assert check(401) == "login_required"
    assert check(200, BIG_PAGE, final_url="https://careers.example.vn/dang-nhap?next=/jobs") == (
        "login_required"
    )
    assert check(200, BIG_PAGE, final_url="https://id.example.vn/account/login") == (
        "login_required"
    )


def test_login_path_requested_directly_is_not_a_redirect() -> None:
    url = "https://example.vn/login"
    assert detect_block(200, {}, BIG_PAGE, requested_url=url, final_url=url) is None


def test_plain_403() -> None:
    assert check(403, "Forbidden") == "http_403"
