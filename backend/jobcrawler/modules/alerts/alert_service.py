"""Cảnh báo chủ động: leader scheduler (`bootstrap/scheduler.py`) gọi `check` mỗi
ALERT_CHECK_SECONDS, đọc các số đo sẵn có và gửi tin tới ALERT_WEBHOOK_URL khi hệ thống chậm / lỗi.

| Loại        | Báo khi                                                                  |
| ----------- | ------------------------------------------------------------------------ |
| `detection` | trong khung poll, P95 cửa sổ phát hiện tin mới 30 phút qua > ngưỡng      |
| `build`     | trong 30 phút sau khung build, hàng đợi build còn việc (mỗi ngày một lần) |
| `workers`   | số worker lấy trang còn kết nối < ALERT_MIN_WORKERS                      |
| `http`      | tỉ lệ slot HTTP đang dùng trên mọi worker > ngưỡng                       |
| `llm`       | tỉ lệ lời gọi LLM lỗi trong giờ gần nhất > ngưỡng                        |
| `partner`   | webhook tới một hệ thống ngoài lỗi liên tục > ALERT_WEBHOOK_FAILING_MINUTES |

Mỗi loại một trạng thái trên Redis chính (`alerts:<loại>`, kv trên `res.redis`): bắt đầu lỗi → gửi,
còn lỗi → nhắc lại sau ALERT_REPEAT_MINUTES, hết lỗi → gửi "đã ổn". Không đủ số liệu (ngoài khung
poll, ít mẫu, LLM tắt) thì giữ nguyên trạng thái. Gửi lỗi thì lần kiểm sau gửi lại.

Webhook nhận JSON `{"text": ...}` (Slack, Mattermost, Google Chat, Microsoft Teams); URL của
Telegram (`api.telegram.org/bot<token>/sendMessage?chat_id=<id>`) thì gửi `chat_id` trong body. Gọi
thẳng từ backend qua `res.api_http` như SERP (không qua worker).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

from sqlalchemy import select

from jobcrawler.common.logging import get_logger
from jobcrawler.database.models import ApiClient
from jobcrawler.modules.crawl import schedule_service, sla_service
from jobcrawler.modules.gateway.registry_service import Keys, list_workers
from jobcrawler.services import llm_metrics
from jobcrawler.services.net.kv import KeyValueStore, RedisKV
from jobcrawler.services.resources import Resources

log = get_logger(__name__)

DETECTION_MINUTES = 30
MIN_POLLS = 20  # ít lượt poll đủ hơn thì P95 chưa nói lên gì
MIN_LLM_CALLS = 20
BUILD_GRACE_MINUTES = 30
STATE_TTL = 7 * 86_400


@dataclass(frozen=True, slots=True)
class Finding:
    kind: str
    # True: đang lỗi; False: ổn; None: không đủ số liệu (giữ trạng thái).
    firing: bool | None
    message: str = ""
    # Chỉ báo một lần cho mỗi giá trị `once` (vd. ngày của khung build), không có tin "đã ổn".
    once: str | None = None


Probe = Callable[[Resources, datetime], Awaitable[list[Finding]]]


async def detection(res: Resources, now: datetime) -> list[Finding]:
    s = res.settings
    if not s.alert_detection_p95_seconds or not schedule_service.poll_window(s).contains(now):
        return [Finding("detection", None)]
    stats = await sla_service.poll_stats(
        res.redis, s.redis_prefix, minutes=DETECTION_MINUTES, now=now.timestamp()
    )
    if sum(stats.window_histogram) < MIN_POLLS or stats.window_p95 is None:
        return [Finding("detection", None)]
    p95 = stats.window_p95
    return [
        Finding(
            "detection",
            p95 > s.alert_detection_p95_seconds,
            f"P95 độ trễ phát hiện tin mới {DETECTION_MINUTES} phút qua: {p95:g} s (ngưỡng "
            f"{s.alert_detection_p95_seconds:g} s, {stats.polls} lượt poll)",
        )
    ]


async def build_backlog(res: Resources, now: datetime) -> list[Finding]:
    s = res.settings
    window = schedule_service.build_window(s)
    if not s.alert_build_backlog or window.minutes == schedule_service.DAY:
        return [Finding("build", None)]
    # Lần kết thúc khung build gần nhất trước `now`: khung đó bắt đầu sau now − độ dài − grace.
    length = timedelta(minutes=window.minutes)
    grace = timedelta(minutes=BUILD_GRACE_MINUTES)
    ended = window.next_start(now - length - grace) + length
    if not ended <= now < ended + grace:
        return [Finding("build", None)]
    info = await res.queues.build.info()
    left = int(info["queued"]) + int(info["active"])
    return [
        Finding(
            "build",
            left > 0,
            f"Khung build kết thúc lúc {ended.astimezone(window.tz):%H:%M} mà hàng đợi build còn "
            f"{left} việc ({info['active']} đang chạy): cần thêm runner build / máy render, hoặc "
            "LLM đang chậm",
            once=f"{ended:%Y-%m-%d}",
        )
    ]


async def workers(res: Resources, now: datetime) -> list[Finding]:
    s = res.settings
    listed = await list_workers(res.redis, Keys(s.redis_prefix), include_gone=False)
    alive = [w for w in listed if w.alive]
    out: list[Finding] = []
    if s.alert_min_workers:
        out.append(
            Finding(
                "workers",
                len(alive) < s.alert_min_workers,
                f"Còn {len(alive)} worker lấy trang kết nối (tối thiểu {s.alert_min_workers})",
            )
        )
    slots = sum(w.http_slots for w in alive)
    if s.alert_http_slot_usage and slots:
        used = sum(w.stats.http_inflight for w in alive)
        out.append(
            Finding(
                "http",
                used / slots > s.alert_http_slot_usage,
                f"Slot HTTP đang dùng {used}/{slots} ({used / slots:.0%}, ngưỡng "
                f"{s.alert_http_slot_usage:.0%}): cần thêm worker",
            )
        )
    return out


async def llm(res: Resources, now: datetime) -> list[Finding]:
    s = res.settings
    if not s.alert_llm_error_rate or not s.llm_enabled:
        return [Finding("llm", None)]
    stats = await llm_metrics.collect(res.redis, s, hours=1, now=now.timestamp())
    called = stats.calls - stats.cached
    if called < MIN_LLM_CALLS:
        return [Finding("llm", None)]
    rate = stats.failed / called
    top = ", ".join(f"{kind} ×{n}" for kind, n in stats.errors[:3])
    return [
        Finding(
            "llm",
            rate > s.alert_llm_error_rate,
            f"LLM lỗi {stats.failed}/{called} lời gọi giờ qua ({rate:.0%}, ngưỡng "
            f"{s.alert_llm_error_rate:.0%})" + (f": {top}" if top else ""),
        )
    ]


async def partner_webhooks(res: Resources, now: datetime) -> list[Finding]:
    """Webhook tới hệ thống ngoài (`modules/integration/webhook_service.py`) lỗi liên tục quá lâu:
    việc làm mới không tới được bên kia."""
    minutes = res.settings.alert_webhook_failing_minutes
    if not minutes:
        return [Finding("partner", None)]
    async with res.sessionmaker() as session:
        rows = (
            await session.execute(
                select(
                    ApiClient.name, ApiClient.webhook_failing_since, ApiClient.webhook_last_error
                )
                .where(
                    ApiClient.webhook_enabled.is_(True),
                    ApiClient.webhook_failing_since.is_not(None),
                    ApiClient.webhook_failing_since < now - timedelta(minutes=minutes),
                )
                .order_by(ApiClient.name)
            )
        ).all()
    detail = "; ".join(f"{name}: {error or 'lỗi'}" for name, _, error in rows[:5])
    return [
        Finding(
            "partner",
            bool(rows),
            f"Webhook tới {len(rows)} hệ thống ngoài lỗi liên tục quá {minutes:g} phút ({detail})",
        )
    ]


PROBES: tuple[Probe, ...] = (detection, build_backlog, workers, llm, partner_webhooks)


def webhook_request(url: str, text: str) -> tuple[str, dict[str, Any]]:
    """(URL, body JSON) để gửi một tin tới webhook."""
    parts = urlsplit(url)
    if parts.hostname == "api.telegram.org":
        chat = parse_qs(parts.query).get("chat_id", [None])[0]
        return urlunsplit(parts._replace(query="")), {"chat_id": chat, "text": text}
    return url, {"text": text}


async def send(res: Resources, text: str) -> bool:
    secret = res.settings.alert_webhook_url
    if secret is None:
        return False
    url, body = webhook_request(secret.get_secret_value(), text)
    result = await res.api_http.fetch(
        url,
        method="POST",
        json=body,
        source="alerts",
        use_cache=False,
        check_robots=False,
        timeout=10,
        retries=1,
    )
    ok = result.status is not None and 200 <= result.status < 300
    if not ok:
        log.warning("alerts.send_failed", status=result.status, error=result.error)
    return ok


def _state_key(kind: str) -> str:
    return f"alerts:{kind}"


def decide(
    finding: Finding, state: dict[str, Any] | None, now: datetime, repeat_minutes: float
) -> tuple[str | None, dict[str, Any] | None]:
    """(tin cần gửi hoặc None, trạng thái mới — None là xoá) của một loại cảnh báo."""
    ts = now.timestamp()
    if finding.once is not None:
        if not finding.firing or (state and state.get("once") == finding.once):
            return None, state
        return f"⚠️ {finding.message}", {"once": finding.once, "sent": ts}
    if finding.firing is None:
        return None, state
    if not finding.firing:
        return (f"✅ Đã ổn: {finding.message}", None) if state else (None, None)
    if state is None:
        return f"⚠️ {finding.message}", {"since": ts, "sent": ts}
    if repeat_minutes and ts - state["sent"] >= repeat_minutes * 60:
        minutes = int((ts - state["since"]) // 60)
        return f"⚠️ Vẫn còn sau {minutes} phút: {finding.message}", state | {"sent": ts}
    return None, state


async def check(
    res: Resources,
    now: datetime,
    *,
    kv: KeyValueStore | None = None,
    probes: Sequence[Probe] = PROBES,
) -> list[str]:
    """Một lượt kiểm: đọc số đo, gửi các tin cần gửi, ghi trạng thái. Trả các tin đã gửi."""
    s = res.settings
    if s.alert_webhook_url is None:
        return []
    kv = kv or RedisKV(res.redis, prefix=s.redis_prefix)
    findings: list[Finding] = []
    for probe in probes:
        try:
            findings += await probe(res, now)
        except Exception as exc:  # một nguồn số đo lỗi không chặn các loại khác
            log.warning("alerts.probe_failed", probe=probe.__name__, error=str(exc))
    sent: list[str] = []
    for finding in findings:
        raw = await kv.get(_state_key(finding.kind))
        state = json.loads(raw) if raw else None
        message, new_state = decide(finding, state, now, s.alert_repeat_minutes)
        if message is not None:
            if not await send(res, f"[job-crawler] {message}"):
                continue  # giữ trạng thái cũ: lần kiểm sau gửi lại
            sent.append(message)
            log.info("alerts.sent", kind=finding.kind, firing=finding.firing)
        if new_state is None:
            if state is not None:
                await kv.delete(_state_key(finding.kind))
        elif new_state != state:
            await kv.set(_state_key(finding.kind), json.dumps(new_state), ttl=STATE_TTL)
    return sent
