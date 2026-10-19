from __future__ import annotations

import pytest

from jobcrawler.net.urls import (
    normalize_url,
    parse_domain_input,
    registrable_domain,
    same_site,
    site_root,
)


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("careers.fpt.com.vn", "fpt.com.vn"),
        ("www.vng.com.vn", "vng.com.vn"),
        ("tuyendung.tiki.vn", "tiki.vn"),
        ("base.vn", "base.vn"),
        ("intel.wd1.myworkdayjobs.com", "myworkdayjobs.com"),
        ("https://www.momo.vn/x/y", "momo.vn"),
        ("shop.example.co.uk", "example.co.uk"),
        ("localhost", "localhost"),
    ],
)
def test_registrable_domain(host: str, expected: str) -> None:
    assert registrable_domain(host) == expected


def test_same_site_includes_subdomains_only() -> None:
    assert same_site("https://careers.base.vn/job/1", "https://base.vn/")
    assert same_site("https://life.base.vn/x", "base.vn")
    assert not same_site("https://momo.careers/", "https://www.momo.vn/")
    assert not same_site("https://evil-base.vn/", "https://base.vn/")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("base.vn", "base.vn"),
        ("https://www.Base.vn/careers", "base.vn"),
        ("  http://tuyendung.vnpay.vn  ", "tuyendung.vnpay.vn"),
        ("www.misa.vn/tuyen-dung/", "misa.vn"),
    ],
)
def test_parse_domain_input(raw: str, expected: str) -> None:
    assert parse_domain_input(raw) == expected


def test_normalize_url() -> None:
    assert normalize_url("/job/1#apply", "https://Careers.Base.vn:443/careers") == (
        "https://careers.base.vn/job/1"
    )
    assert normalize_url("HTTPS://a.vn") == "https://a.vn/"
    assert normalize_url("https://a.vn/x?utm_source=fb&id=3&fbclid=1") == "https://a.vn/x?id=3"
    assert normalize_url("http://a.vn:8080/x") == "http://a.vn:8080/x"
    assert normalize_url("mailto:hr@a.vn") is None
    assert normalize_url("javascript:void(0)") is None
    assert normalize_url("https://[bad") is None
    assert site_root("https://a.vn/x/y?z=1") == "https://a.vn/"
