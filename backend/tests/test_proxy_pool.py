from __future__ import annotations

import random
from collections import Counter
from pathlib import Path

import pytest

from jobcrawler.services.net.kv import MemoryKV
from jobcrawler.services.net.proxy_pool import (
    NoProxyAvailableError,
    Proxy,
    ProxyParseError,
    ProxyPool,
    load_proxies,
    parse_proxy_line,
)

from .conftest import FakeClock


def _p(line: str) -> Proxy:
    proxy = parse_proxy_line(line)
    assert proxy is not None
    return proxy


@pytest.mark.parametrize(
    ("line", "url"),
    [
        ("http://u:p@1.2.3.4:8080", "http://u:p@1.2.3.4:8080"),
        ("socks5h://1.2.3.4:1080  # nhà cung cấp A", "socks5h://1.2.3.4:1080"),
        ("1.2.3.4:3128", "http://1.2.3.4:3128"),
    ],
)
def test_parse_proxy_line(line: str, url: str) -> None:
    assert _p(line).url == url


def test_parse_host_port_user_pass_quotes_credentials() -> None:
    proxy = _p("1.2.3.4:3128:user:p@ss")
    assert proxy.url == "http://user:p%40ss@1.2.3.4:3128"
    assert proxy.playwright() == {
        "server": "http://1.2.3.4:3128",
        "username": "user",
        "password": "p@ss",
    }
    assert proxy.display == "http://user:***@1.2.3.4:3128"


@pytest.mark.parametrize("line", ["", "   ", "# comment"])
def test_parse_skips_blank_and_comments(line: str) -> None:
    assert parse_proxy_line(line) is None


@pytest.mark.parametrize("line", ["ftp://1.2.3.4:21", "http://1.2.3.4", "1.2.3.4", "a:b:c"])
def test_parse_rejects_invalid(line: str) -> None:
    with pytest.raises(ProxyParseError):
        parse_proxy_line(line)


@pytest.mark.parametrize(
    ("line", "shown"),
    [
        ("http://user:mat-khau@1.2.3.4:abc", "http://user:***@1.2.3.4:abc"),
        ("socks5://user:mat-khau@:1080", "socks5://user:***@:1080"),
        ("http://tokenmat-khau@1.2.3.4:x", "http://***@1.2.3.4:x"),
        ("1.2.3.4:abc:user:mat-khau", "http://user:***@1.2.3.4:abc"),
        ("1.2.3.4:3128:user", "1.2.3.4:3128:***"),
        ("1.2.3.4:3128:user:mat-khau:x", "1.2.3.4:3128:***"),
    ],
)
def test_parse_error_masks_credentials(line: str, shown: str) -> None:
    # Thông báo lỗi được log (proxy.invalid_line): không được chứa mật khẩu proxy.
    with pytest.raises(ProxyParseError) as info:
        parse_proxy_line(line)
    assert "mat-khau" not in str(info.value)
    assert repr(shown) in str(info.value)


def test_id_is_stable_and_hides_credentials() -> None:
    a, b = _p("http://u:secret@1.2.3.4:8080"), _p("http://u:secret@1.2.3.4:8080")
    assert a.id == b.id
    assert "secret" not in a.id
    assert "secret" not in a.display


def test_browser_compatibility() -> None:
    assert _p("http://u:p@1.2.3.4:1").browser_compatible
    assert _p("socks5://1.2.3.4:1").browser_compatible
    assert not _p("socks5://u:p@1.2.3.4:1").browser_compatible


def test_load_proxies_dedups_and_skips_bad_lines(tmp_path: Path) -> None:
    f = tmp_path / "proxies.txt"
    f.write_text(
        "# danh sách\n1.2.3.4:8080\nhttp://1.2.3.4:8080\nrác\nsocks5://5.6.7.8:1080\n",
        encoding="utf-8",
    )
    proxies = load_proxies(f)
    assert [p.url for p in proxies] == ["http://1.2.3.4:8080", "socks5://5.6.7.8:1080"]
    assert load_proxies(tmp_path / "missing.txt") == []


def _pool(clock: FakeClock, *lines: str, **kwargs: float) -> ProxyPool:
    proxies = [_p(line) for line in lines]
    return ProxyPool(MemoryKV(clock=clock), proxies, rng=random.Random(1), **kwargs)  # pyright: ignore[reportArgumentType]


async def test_no_proxies_means_direct(clock: FakeClock) -> None:
    pool = _pool(clock)
    assert not pool.enabled
    assert await pool.acquire("a.vn") is None


async def test_sticky_per_domain(clock: FakeClock) -> None:
    pool = _pool(clock, "1.1.1.1:1", "2.2.2.2:2", "3.3.3.3:3")
    first = await pool.acquire("a.vn")
    assert all([await pool.acquire("a.vn") == first for _ in range(10)])
    clock.now += 1801  # hết sticky → có thể chọn lại
    assert await pool.acquire("a.vn") is not None


async def test_blocked_proxy_rests_only_for_that_domain(clock: FakeClock) -> None:
    pool = _pool(clock, "1.1.1.1:1", "2.2.2.2:2")
    first = await pool.acquire("a.vn")
    assert first is not None
    await pool.report_blocked(first, "a.vn")
    second = await pool.acquire("a.vn")
    assert second is not None
    assert second != first
    assert first in [await pool.acquire(f"other{i}.vn") for i in range(20)]
    clock.now += 301  # hết thời gian nghỉ
    await pool.report_blocked(second, "a.vn")
    assert await pool.acquire("a.vn") == first


async def test_consecutive_failures_kill_proxy(clock: FakeClock) -> None:
    pool = _pool(clock, "1.1.1.1:1", max_failures=3)
    proxy = pool.proxies[0]
    await pool.report_failure(proxy, "a.vn")
    await pool.report_success(proxy, "a.vn")  # thành công thì đếm lại từ đầu
    for _ in range(2):
        await pool.report_failure(proxy, "a.vn")
    assert await pool.acquire("b.vn") == proxy
    await pool.report_failure(proxy, "a.vn")
    with pytest.raises(NoProxyAvailableError):
        await pool.acquire("b.vn")
    [state] = await pool.states()
    assert state.dead
    await pool.mark_healthy(proxy)
    assert await pool.acquire("b.vn") == proxy
    [state] = await pool.states()
    assert (state.dead, state.fails, state.score) == (False, 0, 50)


async def test_dead_proxy_returns_after_ttl(clock: FakeClock) -> None:
    pool = _pool(clock, "1.1.1.1:1", dead_seconds=60)
    await pool.mark_dead(pool.proxies[0], reason="test")
    with pytest.raises(NoProxyAvailableError):
        await pool.acquire("a.vn")
    clock.now += 61
    assert await pool.acquire("a.vn") == pool.proxies[0]


async def test_scores_are_clamped(clock: FakeClock) -> None:
    pool = _pool(clock, "1.1.1.1:1", max_failures=100)
    proxy = pool.proxies[0]
    for _ in range(80):
        await pool.report_success(proxy, "a.vn")
    assert (await pool.states())[0].score == 100
    for _ in range(20):
        await pool.report_failure(proxy, "a.vn")
    assert (await pool.states())[0].score == 0


async def test_higher_score_is_preferred(clock: FakeClock) -> None:
    pool = _pool(clock, "1.1.1.1:1", "2.2.2.2:2", max_failures=1000)
    good, bad = pool.proxies
    for _ in range(50):
        await pool.report_success(good, "x")
        await pool.report_failure(bad, "x")
    picks = Counter([await pool.acquire(f"d{i}.vn") for i in range(300)])
    assert picks[good] > 280


async def test_browser_acquire_skips_socks_with_auth(clock: FakeClock) -> None:
    pool = _pool(clock, "socks5://u:p@1.1.1.1:1")
    assert await pool.acquire("a.vn") is not None
    with pytest.raises(NoProxyAvailableError):
        await pool.acquire("b.vn", browser=True)
