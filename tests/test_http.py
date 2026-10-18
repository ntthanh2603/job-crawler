from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from jobcrawler.fakes import FakeRenderer, FakeTransport, response
from jobcrawler.net.http import (
    HttpClient,
    HttpConfig,
    decode_body,
    parse_retry_after,
)
from jobcrawler.net.http import TransportError as TErr
from jobcrawler.net.kv import MemoryKV
from jobcrawler.net.proxy_pool import ProxyPool, parse_proxy_line
from jobcrawler.net.ratelimit import MemoryRateLimiter
from jobcrawler.schemas import FetchOutcome

from .conftest import FakeClock

SITE = "https://jobs.example.vn"
ROBOTS = f"{SITE}/robots.txt"


@dataclass
class Rig:
    client: HttpClient
    transport: FakeTransport
    pool: ProxyPool
    limiter: MemoryRateLimiter
    clock: FakeClock
    backoffs: list[float]  # các lần chờ retry của client (không gồm chờ rate limit)


def make(
    clock: FakeClock,
    routes: dict | None = None,  # pyright: ignore[reportMissingTypeArgument]
    *,
    proxies: tuple[str, ...] = (),
    renderer: FakeRenderer | None = None,
    max_failures: int = 5,
    **config: object,
) -> Rig:
    kv = MemoryKV(clock=clock)
    pool = ProxyPool(
        kv,
        [p for line in proxies if (p := parse_proxy_line(line))],
        rng=random.Random(0),
        max_failures=max_failures,
    )
    limiter = MemoryRateLimiter(clock=clock, sleep=clock.sleep)
    transport = FakeTransport(routes)
    backoffs: list[float] = []

    async def backoff_sleep(seconds: float) -> None:
        backoffs.append(seconds)
        clock.now += seconds

    client = HttpClient(
        transport,
        kv=kv,
        proxies=pool,
        ratelimiter=limiter,
        renderer=renderer,
        config=HttpConfig(**config),  # pyright: ignore[reportArgumentType]
        sleep=backoff_sleep,
        rng=random.Random(0),
    )
    return Rig(client, transport, pool, limiter, clock, backoffs)


async def test_fetch_ok_then_served_from_cache(clock: FakeClock) -> None:
    url = f"{SITE}/a"
    rig = make(clock, {url: response(url, "<p>Tuyển dụng</p>")})
    first = await rig.client.fetch(url, source="careers")
    assert first.ok
    assert (first.status, first.text, first.source, first.attempts) == (
        200,
        "<p>Tuyển dụng</p>",
        "careers",
        1,
    )
    assert rig.transport.urls() == [ROBOTS, url]

    second = await rig.client.fetch(url)
    assert second.from_cache
    assert second.text == "<p>Tuyển dụng</p>"
    assert rig.transport.urls() == [ROBOTS, url]  # không gọi lại


async def test_sends_accept_language_and_leaves_user_agent_to_impersonation(
    clock: FakeClock,
) -> None:
    url = f"{SITE}/a"
    rig = make(clock, {url: response(url, "x")})
    await rig.client.fetch(url, headers={"X-Test": "1"})
    headers = rig.transport.calls[-1].headers
    assert headers["Accept-Language"].startswith("vi-VN")
    assert headers["X-Test"] == "1"
    assert not any(k.lower() == "user-agent" for k in headers)


async def test_params_are_appended(clock: FakeClock) -> None:
    url = f"{SITE}/search?q=python&page=2"
    rig = make(clock, {url: response(url, "ok")})
    result = await rig.client.fetch(f"{SITE}/search", params={"q": "python", "page": 2})
    assert result.ok
    assert result.url == url


async def test_robots_disallow_skips_request(clock: FakeClock) -> None:
    rig = make(clock, {ROBOTS: response(ROBOTS, "User-agent: *\nDisallow: /private\n")})
    result = await rig.client.fetch(f"{SITE}/private/1")
    assert result.outcome is FetchOutcome.ROBOTS_DISALLOWED
    assert result.error == "robots_disallowed"
    assert rig.transport.urls() == [ROBOTS]


async def test_robots_can_be_skipped(clock: FakeClock) -> None:
    url = f"{SITE}/private/1"
    rig = make(
        clock,
        {ROBOTS: response(ROBOTS, "User-agent: *\nDisallow: /\n"), url: response(url, "ok")},
    )
    assert (await rig.client.fetch(url, check_robots=False)).ok


async def test_robots_server_error_disallows_all(clock: FakeClock) -> None:
    rig = make(clock, {ROBOTS: response(ROBOTS, "", status=503)})
    result = await rig.client.fetch(f"{SITE}/a")
    assert result.outcome is FetchOutcome.ROBOTS_DISALLOWED
    assert result.error == "robots_unreachable"  # không lẫn với luật cấm trong robots.txt


async def test_crawl_delay_and_domain_override_set_rate(clock: FakeClock) -> None:
    url = f"{SITE}/a"
    rig = make(
        clock,
        {ROBOTS: response(ROBOTS, "User-agent: *\nCrawl-delay: 5\n"), url: response(url, "ok")},
    )
    await rig.client.fetch(url)
    assert rig.limiter.calls[-1] == ("jobs.example.vn", 0.2, 1)

    other = "https://slow.example.vn/a"
    rig2 = make(clock, {other: response(other, "ok")}, rate_overrides={"slow.example.vn": 0.25})
    await rig2.client.fetch(other)
    assert rig2.limiter.calls[-1][1] == 0.25


async def test_retries_5xx_with_backoff(clock: FakeClock) -> None:
    url = f"{SITE}/a"
    rig = make(
        clock,
        {url: [response(url, "", status=503), response(url, "", status=502), response(url, "ok")]},
    )
    result = await rig.client.fetch(url)
    assert result.ok
    assert result.attempts == 3
    # backoff 1s, 2s × jitter [0.5, 1.0]
    assert len(rig.backoffs) == 2
    assert 0.5 <= rig.backoffs[0] <= 1.0
    assert 1.0 <= rig.backoffs[1] <= 2.0


async def test_5xx_exhausted_is_error(clock: FakeClock) -> None:
    url = f"{SITE}/a"
    rig = make(clock, {url: response(url, "", status=500)}, max_retries=2)
    result = await rig.client.fetch(url)
    assert (result.outcome, result.error, result.attempts) == (FetchOutcome.ERROR, "http_500", 3)
    assert rig.transport.urls().count(url) == 3


async def test_429_respects_retry_after_and_rotates_proxy(clock: FakeClock) -> None:
    url = f"{SITE}/a"
    rig = make(
        clock,
        {url: [response(url, "", status=429, headers={"Retry-After": "7"}), response(url, "ok")]},
        proxies=("1.1.1.1:1", "2.2.2.2:2"),
    )
    result = await rig.client.fetch(url)
    assert result.ok
    assert rig.backoffs == [7]
    first_proxy, second_proxy = [c.proxy for c in rig.transport.calls if c.url == url]
    assert first_proxy != second_proxy  # proxy bị 429 được cho nghỉ với domain này


async def test_429_with_long_retry_after_stops_as_blocked(clock: FakeClock) -> None:
    url = f"{SITE}/a"
    rig = make(clock, {url: response(url, "", status=429, headers={"Retry-After": "120"})})
    result = await rig.client.fetch(url)
    assert (result.outcome, result.error, result.attempts) == (
        FetchOutcome.BLOCKED,
        "rate_limited",
        1,
    )


async def test_timeouts_retry_then_report_timeout(clock: FakeClock) -> None:
    url = f"{SITE}/a"
    rig = make(clock, {url: TErr("timeout", "operation timed out")})
    result = await rig.client.fetch(url)
    assert result.outcome is FetchOutcome.TIMEOUT
    assert result.attempts == 4
    assert len(rig.backoffs) == 3
    assert not (await rig.client.fetch(url, use_cache=False)).ok


async def test_retries_can_be_turned_off_per_request(clock: FakeClock) -> None:
    url = f"{SITE}/a"
    rig = make(clock, {url: TErr("timeout", "operation timed out")})
    result = await rig.client.fetch(url, retries=0)  # URL đoán: không thử lại
    assert result.outcome is FetchOutcome.TIMEOUT
    assert result.attempts == 1
    assert rig.backoffs == []


async def test_block_page_stops_immediately_and_cools_proxy(clock: FakeClock) -> None:
    url = f"{SITE}/a"
    rig = make(
        clock,
        {url: response(url, "", status=403, headers={"cf-mitigated": "challenge"})},
        proxies=("1.1.1.1:1", "2.2.2.2:2"),
    )
    result = await rig.client.fetch(url)
    assert (result.outcome, result.error, result.attempts) == (
        FetchOutcome.BLOCKED,
        "cloudflare_challenge",
        1,
    )
    blocked = next(p for p in rig.pool.proxies if p.id == result.proxy_id)
    assert await rig.pool.acquire("jobs.example.vn") != blocked


async def test_login_redirect_is_blocked(clock: FakeClock) -> None:
    url = f"{SITE}/jobs"
    rig = make(clock, {url: response(url, "<form>", final_url=f"{SITE}/login?next=/jobs")})
    result = await rig.client.fetch(url)
    assert (result.outcome, result.error) == (FetchOutcome.BLOCKED, "login_required")


async def test_404_is_error_without_retry_or_cache(clock: FakeClock) -> None:
    url = f"{SITE}/careers"
    rig = make(clock)
    result = await rig.client.fetch(url)
    assert (result.outcome, result.status, result.error, result.attempts) == (
        FetchOutcome.ERROR,
        404,
        "http_404",
        1,
    )
    await rig.client.fetch(url)
    assert rig.transport.urls().count(url) == 2


async def test_post_is_not_cached(clock: FakeClock) -> None:
    url = f"{SITE}/api/jobs"
    rig = make(clock, {url: response(url, '{"jobs": []}', content_type="application/json")})
    for _ in range(2):
        result = await rig.client.fetch(url, method="POST", json={"page": 1})
        assert result.json() == {"jobs": []}
    assert rig.transport.urls().count(url) == 2
    assert rig.transport.calls[-1].json == {"page": 1}


async def test_proxy_failures_kill_proxy(clock: FakeClock) -> None:
    url = f"{SITE}/a"
    rig = make(
        clock, {url: TErr("proxy", "CONNECT failed")}, proxies=("1.1.1.1:1",), max_failures=2
    )
    result = await rig.client.fetch(url, check_robots=False)
    assert result.outcome is FetchOutcome.ERROR
    assert "không còn proxy" in (result.error or "")
    [state] = await rig.pool.states()
    assert state.dead


async def test_no_proxy_goes_direct(clock: FakeClock) -> None:
    url = f"{SITE}/a"
    rig = make(clock, {url: response(url, "ok")})
    await rig.client.fetch(url)
    assert {c.proxy for c in rig.transport.calls} == {None}


async def test_proxy_is_used_when_configured(clock: FakeClock) -> None:
    url = f"{SITE}/a"
    rig = make(clock, {url: response(url, "ok")}, proxies=("http://u:p@1.1.1.1:8080",))
    result = await rig.client.fetch(url)
    assert {c.proxy for c in rig.transport.calls} == {"http://u:p@1.1.1.1:8080"}
    assert result.proxy_id == rig.pool.proxies[0].id


async def test_render_ok_and_blocked(clock: FakeClock) -> None:
    ok_url, bad_url = f"{SITE}/spa", f"{SITE}/protected"
    renderer = FakeRenderer(
        {
            ok_url: response(ok_url, "<div>Job list</div>"),
            bad_url: response(bad_url, "<div class='g-recaptcha'></div>", status=403),
        }
    )
    rig = make(clock, renderer=renderer)
    ok = await rig.client.render(ok_url, source="careers")
    assert ok.ok
    assert ok.rendered
    assert ok.text == "<div>Job list</div>"
    bad = await rig.client.render(bad_url)
    assert (bad.outcome, bad.error) == (FetchOutcome.BLOCKED, "captcha")
    assert [u for u, _ in renderer.calls] == [ok_url, bad_url]


async def test_render_respects_robots_and_needs_browser(clock: FakeClock) -> None:
    rig = make(
        clock, {ROBOTS: response(ROBOTS, "User-agent: *\nDisallow: /\n")}, renderer=FakeRenderer()
    )
    assert (await rig.client.render(f"{SITE}/x")).outcome is FetchOutcome.ROBOTS_DISALLOWED
    no_browser = make(clock)
    result = await no_browser.client.render(f"{SITE}/x")
    assert (result.outcome, result.error) == (FetchOutcome.ERROR, "browser_unavailable")


def test_parse_retry_after() -> None:
    now = datetime(2026, 10, 16, 10, 0, 0, tzinfo=UTC)
    assert parse_retry_after("30") == 30
    assert parse_retry_after("Fri, 16 Oct 2026 10:00:45 GMT", now=now) == 45
    assert parse_retry_after("Fri, 16 Oct 2026 09:00:00 GMT", now=now) == 0
    assert parse_retry_after("soon") is None
    assert parse_retry_after(None) is None


@pytest.mark.parametrize(
    ("content", "content_type", "expected"),
    [
        ("Tuyển dụng".encode(), "text/html; charset=utf-8", "Tuyển dụng"),
        ("Tuyển dụng".encode("utf-16"), "text/html; charset=utf-16", "Tuyển dụng"),
        (
            b'<meta charset="windows-1252"><p>caf\xe9</p>',
            "text/html",
            '<meta charset="windows-1252"><p>café</p>',
        ),
        (b"\xff\xfe broken", None, "�� broken"),
    ],
)
def test_decode_body(content: bytes, content_type: str | None, expected: str) -> None:
    assert decode_body(content, content_type) == expected
