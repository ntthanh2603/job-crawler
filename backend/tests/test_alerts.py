"""Cảnh báo chủ động (modules/alerts/alert_service.py): trạng thái gửi / nhắc lại / đã ổn, định dạng
webhook, vòng kiểm với nguồn số đo giả (không gọi mạng ngoài)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from jobcrawler.common.config import Settings
from jobcrawler.modules.alerts import alert_service
from jobcrawler.modules.alerts.alert_service import Finding, check, decide, webhook_request
from jobcrawler.services.net.http import FetchOutcome, FetchResult
from jobcrawler.services.net.kv import MemoryKV

pytestmark = pytest.mark.usefixtures("clean_env")

NOW = datetime(2026, 10, 22, 3, 0, tzinfo=UTC)  # 10:00 giờ Việt Nam: trong khung poll


class FakeApi:
    """`res.api_http` giả: ghi lại request, trả mã `status`."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.calls: list[tuple[str, Any]] = []

    async def fetch(self, url: str, **kwargs: Any) -> FetchResult:
        self.calls.append((url, kwargs["json"]))
        return FetchResult(url=url, final_url=url, status=self.status, outcome=FetchOutcome.OK)


def resources(api: FakeApi, **settings: Any) -> Any:
    values: dict[str, Any] = {"alert_webhook_url": SecretStr("https://hooks.example/x")}
    return SimpleNamespace(settings=Settings(**(values | settings)), api_http=api)


def probe(*findings: Finding) -> Any:
    async def run(_res: Any, _now: datetime) -> list[Finding]:
        return list(findings)

    return run


def test_decide_fires_repeats_and_resolves() -> None:
    bad = Finding("detection", True, "P95 240 s")
    message, state = decide(bad, None, NOW, 60)
    assert message == "⚠️ P95 240 s"
    assert state is not None
    # Chưa tới lúc nhắc lại.
    assert decide(bad, state, NOW + timedelta(minutes=30), 60) == (None, state)
    message, again = decide(bad, state, NOW + timedelta(minutes=61), 60)
    assert message == "⚠️ Vẫn còn sau 61 phút: P95 240 s"
    assert again is not None
    assert again["since"] == state["since"]
    # 0: không nhắc lại.
    assert decide(bad, state, NOW + timedelta(days=1), 0) == (None, state)
    # Không đủ số liệu: giữ nguyên.
    assert decide(Finding("detection", None), state, NOW, 60) == (None, state)
    # Hết lỗi: báo đã ổn rồi xoá trạng thái; chưa từng lỗi thì im lặng.
    ok = Finding("detection", False, "P95 150 s")
    assert decide(ok, state, NOW, 60) == ("✅ Đã ổn: P95 150 s", None)
    assert decide(ok, None, NOW, 60) == (None, None)


def test_decide_once_per_day() -> None:
    late = Finding("build", True, "còn 12 việc", once="2026-10-22")
    message, state = decide(late, None, NOW, 60)
    assert message == "⚠️ còn 12 việc"
    assert decide(late, state, NOW + timedelta(minutes=5), 60) == (None, state)
    tomorrow = Finding("build", True, "còn 3 việc", once="2026-10-23")
    assert decide(tomorrow, state, NOW + timedelta(days=1), 60)[0] == "⚠️ còn 3 việc"
    assert decide(Finding("build", False, "", once="2026-10-24"), state, NOW, 60) == (None, state)


def test_webhook_request_formats() -> None:
    assert webhook_request("https://hooks.slack.com/services/T/B/x", "hi") == (
        "https://hooks.slack.com/services/T/B/x",
        {"text": "hi"},
    )
    url, body = webhook_request("https://api.telegram.org/bot123:abc/sendMessage?chat_id=-42", "hi")
    assert url == "https://api.telegram.org/bot123:abc/sendMessage"
    assert body == {"chat_id": "-42", "text": "hi"}


async def test_check_sends_once_and_keeps_state_in_kv() -> None:
    api = FakeApi()
    res = resources(api)
    kv = MemoryKV()
    probes = [probe(Finding("workers", True, "Còn 0 worker"), Finding("llm", None))]
    sent = await check(res, NOW, kv=kv, probes=probes)
    assert sent == ["⚠️ Còn 0 worker"]
    assert api.calls == [("https://hooks.example/x", {"text": "[job-crawler] ⚠️ Còn 0 worker"})]
    raw = await kv.get("alerts:workers")
    assert raw is not None
    assert json.loads(raw)["since"] == NOW.timestamp()
    assert await kv.get("alerts:llm") is None
    # Lượt kiểm sau, vẫn lỗi: không gửi lại trước ALERT_REPEAT_MINUTES.
    assert await check(res, NOW + timedelta(minutes=1), kv=kv, probes=probes) == []
    recovered = [probe(Finding("workers", False, "Còn 2 worker"))]
    assert await check(res, NOW + timedelta(minutes=2), kv=kv, probes=recovered) == [
        "✅ Đã ổn: Còn 2 worker"
    ]
    assert await kv.get("alerts:workers") is None


async def test_failed_send_is_retried_and_broken_probe_is_skipped() -> None:
    api = FakeApi(status=500)
    res = resources(api)
    kv = MemoryKV()

    async def broken(_res: Any, _now: datetime) -> list[Finding]:
        raise RuntimeError("redis down")

    probes = [broken, probe(Finding("http", True, "Slot HTTP 90%"))]
    assert await check(res, NOW, kv=kv, probes=probes) == []
    assert await kv.get("alerts:http") is None  # chưa gửi được: lần sau gửi lại
    api.status = 200
    assert await check(res, NOW, kv=kv, probes=probes) == ["⚠️ Slot HTTP 90%"]


async def test_no_webhook_means_no_checks() -> None:
    api = FakeApi()
    res = resources(api, alert_webhook_url=None)
    called: list[str] = []

    async def spy(_res: Any, _now: datetime) -> list[Finding]:
        called.append("probe")
        return [Finding("workers", True, "x")]

    assert await check(res, NOW, kv=MemoryKV(), probes=[spy]) == []
    assert called == []
    assert api.calls == []


async def test_build_backlog_only_right_after_the_build_window() -> None:
    class Queue:
        async def info(self) -> dict[str, Any]:
            return {"queued": 5, "active": 2}

    api = FakeApi()
    res = resources(api, build_window="00:00-06:00", crawl_timezone="Asia/Ho_Chi_Minh")
    res.queues = SimpleNamespace(build=Queue())
    six = datetime(2026, 10, 21, 23, 0, tzinfo=UTC)  # 06:00 giờ Việt Nam
    [inside] = await alert_service.build_backlog(res, six - timedelta(hours=1))
    assert inside.firing is None
    [late] = await alert_service.build_backlog(res, six + timedelta(minutes=10))
    assert late.firing is True
    assert late.once == "2026-10-21"
    assert "06:00" in late.message
    assert "còn 7 việc" in late.message
    [later] = await alert_service.build_backlog(res, six + timedelta(hours=2))
    assert later.firing is None
