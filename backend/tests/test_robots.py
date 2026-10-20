from __future__ import annotations

import pytest

from jobcrawler.services.net.kv import MemoryKV
from jobcrawler.services.net.robots import RobotsCache, parse_robots, robots_url

from .conftest import FakeClock

BASE = "https://example.vn"


def allowed(robots: str, path: str, agents: tuple[str, ...] = ("*",)) -> bool:
    return parse_robots(robots, agents).allowed(BASE + path)


def test_longest_match_wins() -> None:
    txt = "User-agent: *\nDisallow: /jobs\nAllow: /jobs/public\n"
    assert not allowed(txt, "/jobs/123")
    assert allowed(txt, "/jobs/public/123")
    assert allowed(txt, "/about")


def test_allow_wins_tie() -> None:
    assert allowed("User-agent: *\nDisallow: /a\nAllow: /a\n", "/a/b")


def test_wildcards_and_end_anchor() -> None:
    txt = "User-agent: *\nDisallow: /*?sort=\nDisallow: /*.pdf$\n"
    assert not allowed(txt, "/search?sort=new")
    assert allowed(txt, "/search?q=python")
    assert not allowed(txt, "/files/jd.pdf")
    assert allowed(txt, "/files/jd.pdf?download=1")


def test_empty_disallow_allows_everything() -> None:
    assert allowed("User-agent: *\nDisallow:\n", "/anything")


def test_robots_txt_itself_always_allowed() -> None:
    assert allowed("User-agent: *\nDisallow: /\n", "/robots.txt")
    assert not allowed("User-agent: *\nDisallow: /\n", "/")


def test_group_selection() -> None:
    txt = (
        "User-agent: jobcrawler\nDisallow: /\n\n"
        "User-agent: Googlebot\nUser-agent: *\nDisallow: /admin\n"
    )
    assert allowed(txt, "/jobs")
    assert not allowed(txt, "/admin/x")
    assert not allowed(txt, "/jobs", agents=("JobCrawler",))


def test_rules_before_any_user_agent_are_ignored() -> None:
    assert allowed("Disallow: /\nUser-agent: *\nDisallow: /x\n", "/y")


def test_crawl_delay_sitemaps_comments_case() -> None:
    txt = (
        "# robots\nSITEMAP: https://example.vn/sitemap.xml\n"
        "user-agent: *  # mọi bot\ncrawl-delay: 2.5\nCrawl-delay: abc\nDISALLOW: /tmp\n"
    )
    rules = parse_robots(txt)
    assert rules.crawl_delay == 2.5
    assert rules.sitemaps == ["https://example.vn/sitemap.xml"]
    assert not rules.allowed(BASE + "/tmp/x")


def test_percent_encoding_is_normalized() -> None:
    txt = "User-agent: *\nDisallow: /tuyển-dụng\n"
    assert not allowed(txt, "/tuy%E1%BB%83n-d%E1%BB%A5ng/123")
    assert not allowed(txt, "/tuyển-dụng/123")


def test_robots_url() -> None:
    assert robots_url("https://a.vn:8443/x/y?z=1") == "https://a.vn:8443/robots.txt"


class Fetches:
    def __init__(self, replies: dict[str, tuple[int | None, str]]) -> None:
        self.replies = replies
        self.calls: list[str] = []

    async def __call__(self, url: str) -> tuple[int | None, str]:
        self.calls.append(url)
        return self.replies.get(url, (404, ""))


@pytest.mark.parametrize(
    ("status", "allowed_"),
    [(200, False), (404, True), (403, True), (429, False), (500, False), (None, False)],
)
async def test_cache_status_handling(clock: FakeClock, status: int | None, allowed_: bool) -> None:
    fetch = Fetches({f"{BASE}/robots.txt": (status, "User-agent: *\nDisallow: /private\n")})
    cache = RobotsCache(MemoryKV(clock=clock), fetch, clock=clock)
    rules = await cache.rules_for(BASE + "/private/1")
    assert rules.allowed(BASE + "/private/1") is allowed_


async def test_cache_fetches_once_per_origin(clock: FakeClock) -> None:
    fetch = Fetches({f"{BASE}/robots.txt": (200, "User-agent: *\nDisallow: /x\n")})
    kv = MemoryKV(clock=clock)
    cache = RobotsCache(kv, fetch, clock=clock)
    for path in ("/a", "/b", "/x/1"):
        await cache.rules_for(BASE + path)
    await cache.rules_for("https://other.vn/a")
    assert fetch.calls == [f"{BASE}/robots.txt", "https://other.vn/robots.txt"]

    # Process khác (cache cục bộ rỗng) đọc lại từ KV, không fetch nữa.
    other = RobotsCache(kv, fetch, clock=clock)
    assert not (await other.rules_for(BASE + "/x/2")).allowed(BASE + "/x/2")
    assert len(fetch.calls) == 2

    clock.now += 86_401  # hết hạn 24h → fetch lại
    await RobotsCache(kv, fetch, clock=clock).rules_for(BASE + "/a")
    assert len(fetch.calls) == 3


async def test_server_error_cached_briefly(clock: FakeClock) -> None:
    fetch = Fetches({f"{BASE}/robots.txt": (503, "")})
    kv = MemoryKV(clock=clock)
    await RobotsCache(kv, fetch, clock=clock).rules_for(BASE + "/a")
    clock.now += 601
    await RobotsCache(kv, fetch, clock=clock).rules_for(BASE + "/a")
    assert len(fetch.calls) == 2
