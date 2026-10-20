from __future__ import annotations

from pathlib import Path

import pytest

from jobworker.config import WorkerSettings, split_proxies
from jobworker.proxies import (
    LocalProxyPool,
    Proxy,
    ProxyParseError,
    load_proxies,
    mask,
    parse_proxy_line,
    playwright_settings,
    proxy_id,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make(n: int) -> list[Proxy]:
    return [
        Proxy(id=proxy_id(f"http://10.0.0.{i}:80"), url=f"http://10.0.0.{i}:80") for i in range(n)
    ]


# --- định dạng ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "url"),
    [
        ("1.2.3.4:8080", "http://1.2.3.4:8080"),
        ("1.2.3.4:8080:user:p@ss", "http://user:p%40ss@1.2.3.4:8080"),
        ("socks5h://u:p@proxy.local:1080  # ghi chú", "socks5h://u:p@proxy.local:1080"),
        ("https://1.2.3.4:8443", "https://1.2.3.4:8443"),
    ],
)
def test_parse_proxy_line(line: str, url: str) -> None:
    proxy = parse_proxy_line(line)
    assert proxy is not None
    assert proxy.url == url
    assert proxy.id.startswith("w:")
    assert proxy.id == proxy_id(url)


@pytest.mark.parametrize("line", ["", "   ", "# chỉ ghi chú"])
def test_parse_blank_lines(line: str) -> None:
    assert parse_proxy_line(line) is None


@pytest.mark.parametrize("line", ["ftp://1.2.3.4:21", "1.2.3.4", "http://host:notaport"])
def test_parse_invalid(line: str) -> None:
    with pytest.raises(ProxyParseError):
        parse_proxy_line(line)


def test_mask_hides_password_and_playwright_settings() -> None:
    url = "http://user:s%40cret@1.2.3.4:8080"
    assert mask(url) == "http://user:***@1.2.3.4:8080"
    assert "cret" not in Proxy(id="w:x", url=url).display
    assert playwright_settings(url) == {
        "server": "http://1.2.3.4:8080",
        "username": "user",
        "password": "s@cret",
    }


def test_load_proxies_from_list_and_file(tmp_path: Path) -> None:
    file = tmp_path / "proxies.txt"
    file.write_text("# danh sách\n1.2.3.4:8080\nkhông-hợp-lệ\n5.6.7.8:3128:u:p\n", encoding="utf-8")
    proxies = load_proxies(["http://1.2.3.4:8080", "9.9.9.9:80"], file)
    assert [p.url for p in proxies] == [
        "http://1.2.3.4:8080",
        "http://9.9.9.9:80",
        "http://u:p@5.6.7.8:3128",
    ]
    assert load_proxies([], tmp_path / "khong-co.txt") == []


def test_split_proxies_formats() -> None:
    assert split_proxies('["http://a:1", "b:2"]') == ["http://a:1", "b:2"]
    assert split_proxies("http://a:1, b:2\nc:3\n") == ["http://a:1", "b:2", "c:3"]
    # Chú thích `#` như file proxy (secret `worker_proxies`), kể cả chú thích có dấu phẩy.
    assert split_proxies("# a:1, b:2\r\nc:3  # x, y\n\n") == ["c:3"]
    assert split_proxies(None) == []


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_ADDR", "gw.example:443")
    monkeypatch.setenv("GATEWAY_TOKEN", "bí-mật")
    monkeypatch.setenv("GATEWAY_TLS", "true")
    monkeypatch.setenv("WORKER_PROXIES", "1.2.3.4:80,5.6.7.8:80")
    monkeypatch.setenv("WORKER_LABELS", '{"region": "hn"}')
    monkeypatch.setenv("WORKER_RENDER_SLOTS", "0")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    s = WorkerSettings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert s.gateway_addr == "gw.example:443"
    assert s.gateway_token is not None
    assert s.gateway_token.get_secret_value() == "bí-mật"
    assert s.gateway_tls is True
    assert s.worker_proxies == ["1.2.3.4:80", "5.6.7.8:80"]
    assert s.worker_labels == {"region": "hn"}
    assert s.worker_render_slots == 0
    assert s.log_level == "DEBUG"
    assert s.worker_pool == "default"
    assert s.worker_http_slots is None  # trống = tự tính theo máy
    assert 8 <= s.http_slots <= 64


def test_default_worker_id_is_unique() -> None:
    a = WorkerSettings(_env_file=None)  # pyright: ignore[reportCallIssue]
    b = WorkerSettings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert a.worker_id != b.worker_id
    assert len(a.worker_id.rsplit("-", 1)[1]) == 8


# --- pool ----------------------------------------------------------------------------------------


def test_empty_pool_goes_direct() -> None:
    pool = LocalProxyPool([])
    assert pool.choose("a.vn") is None
    assert pool.alive() == 0


def test_sticky_per_host_and_round_robin() -> None:
    proxies = make(3)
    pool = LocalProxyPool(proxies, clock=Clock())
    first = pool.choose("a.vn")
    assert first is not None
    assert all(pool.choose("a.vn") == first for _ in range(5))
    # Domain khác nhận proxy kế tiếp theo vòng.
    second = pool.choose("b.vn")
    third = pool.choose("c.vn")
    assert len({first.id, second.id if second else "", third.id if third else ""}) == 3
    assert pool.choose("d.vn") == first


def test_sticky_expires() -> None:
    clock = Clock()
    proxies = make(2)
    pool = LocalProxyPool(proxies, sticky_seconds=60, clock=clock)
    first = pool.choose("a.vn")
    assert pool.choose("a.vn") == first
    clock.now += 61
    assert pool.choose("a.vn") != first  # hết sticky: chọn tiếp theo vòng


def test_blocked_cools_down_for_that_host_only() -> None:
    clock = Clock()
    proxies = make(2)
    pool = LocalProxyPool(proxies, cooldown_seconds=120, clock=clock)
    first = pool.choose("a.vn")
    assert first is not None
    assert pool.choose("b.vn") is not None
    pool.report_blocked(first, "a.vn")
    other = pool.choose("a.vn")
    assert other is not None
    assert other != first
    # Với domain khác, proxy vẫn dùng được.
    assert pool.choose("c.vn") is not None
    pool.report_blocked(other, "a.vn")
    assert pool.choose("a.vn") is None  # mọi proxy đang nghỉ với a.vn → đi thẳng
    clock.now += 121
    assert pool.choose("a.vn") is not None


def test_failures_mark_dead_then_revive() -> None:
    clock = Clock()
    [proxy] = make(1)
    pool = LocalProxyPool(
        [proxy], cooldown_seconds=1, max_failures=3, dead_seconds=300, clock=clock
    )
    for _ in range(2):
        pool.report_failure(proxy, "a.vn")
    clock.now += 2
    assert pool.alive() == 1
    pool.report_success(proxy, "a.vn")  # thành công: đếm lại từ đầu
    for _ in range(3):
        pool.report_failure(proxy, "a.vn")
    clock.now += 2
    assert pool.alive() == 0
    assert pool.choose("b.vn") is None
    clock.now += 300
    assert pool.alive() == 1
    assert pool.choose("b.vn") == proxy


def test_browser_skips_socks_with_auth() -> None:
    socks = parse_proxy_line("socks5://u:p@1.1.1.1:1080")
    http = parse_proxy_line("http://u:p@2.2.2.2:8080")
    assert socks is not None
    assert http is not None
    pool = LocalProxyPool([socks, http], clock=Clock())
    assert pool.choose("a.vn") == socks
    # Render cho cùng domain không dùng được proxy SOCKS có mật khẩu.
    assert pool.choose("a.vn", browser=True) == http
