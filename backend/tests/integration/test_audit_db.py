"""Nhật ký thao tác (`audit_events`) trên Postgres + Redis thật: lưu cài đặt (bí mật đã che), thao
tác ở giao diện, API danh sách (lọc + phân trang keyset), bảo trì không dọn nhật ký."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
import structlog
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import delete, select, update

from jobcrawler.bootstrap.app import create_app
from jobcrawler.common.config import Settings
from jobcrawler.database.models import AppSetting, AuditEvent, Company, Job, Log
from jobcrawler.modules.audit import audit_service
from jobcrawler.modules.gateway import registry_service
from jobcrawler.modules.maintenance import maintenance_service
from jobcrawler.modules.settings import app_settings_service
from jobcrawler.services.redis.queues import drop_queue
from jobcrawler.services.resources import Resources

pytestmark = pytest.mark.integration

ORIGIN = {"Origin": "http://ui.test"}
FIELDS = {
    "id",
    "occurred_at",
    "actor",
    "action",
    "resource_type",
    "resource_id",
    "resource_label",
    "request_id",
    "ip",
    "user_agent",
    "changes",
    "metadata",
}


@dataclass
class Web:
    client: httpx.AsyncClient
    settings: Settings
    res: Resources
    tag: str
    user: str  # tên đăng nhập riêng của test: lọc nhật ký theo `actor` không lẫn test khác
    company_id: UUID
    job_id: UUID

    async def events(self, **params: Any) -> list[dict[str, Any]]:
        r = await self.client.get("/api/audit", params={"actor": self.user, "limit": 200, **params})
        assert r.status_code == 200, r.text
        return r.json()["items"]


@pytest.fixture
async def web(integration_settings: Settings) -> AsyncIterator[Web]:
    tag = uuid4().hex[:8]
    user = f"op-{tag}"
    settings = integration_settings.model_copy(
        update={
            "redis_prefix": f"jca{tag}",
            "build_queue_name": f"jobcrawler-test-build-{tag}",
            "poll_queue_name": f"jobcrawler-test-poll-{tag}",
            "board_queue_name": f"jobcrawler-test-boards-{tag}",
            "llm_base_url": None,
            "serp_api_key": None,
            "ui_username": user,
            "ui_password": SecretStr("mat-khau"),
        }
    )
    app = create_app(settings)
    try:
        async with app.router.lifespan_context(app):
            res: Resources = app.state.res
            async with res.sessionmaker.begin() as s:
                await s.execute(delete(AppSetting))
                company = Company(input_name=f"Audit {tag}", legal_name=f"Công ty Audit {tag}")
                s.add(company)
                await s.flush()
                job = Job(
                    company_id=company.id,
                    cluster_key=f"audit-{tag}",
                    title=f"Kỹ sư {tag}",
                    title_normalized="ky su",
                    company_name=f"Công ty Audit {tag}",
                    description_md="Mô tả",
                    apply_url=f"https://audit.vn/jobs/{tag}",
                    canonical_source="careers:audit.vn",
                    confidence=0.9,
                )
                s.add(job)
                await s.flush()
                ids = company.id, job.id
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://ui.test",
                headers={"Authorization": f"Bearer {app.state.token_auth.issue().token}"} | ORIGIN,
            ) as client:
                yield Web(client, settings, res, tag, user, *ids)
            async with res.sessionmaker.begin() as s:
                await s.execute(delete(AppSetting))
    finally:
        redis = Redis.from_url(settings.redis_url)
        try:
            keys = [k async for k in redis.scan_iter(match=f"{settings.redis_prefix}:*")]
            if keys:
                await redis.delete(*keys)
        finally:
            await redis.aclose()
        for name in settings.queue_names:
            await drop_queue(settings.redis_url, name)


async def test_settings_save_is_audited_with_secrets_masked(web: Web) -> None:
    secret = f"sk-{uuid4().hex}"
    proxy_password = f"pw{uuid4().hex[:12]}"
    r = await web.client.put(
        "/api/settings",
        json={
            "values": {
                "poll_interval_seconds": 120,
                "serp_api_key": secret,
                "proxies": [f"http://u1:{proxy_password}@10.0.0.1:8080"],
            }
        },
    )
    assert r.status_code == 200, r.text
    [event] = await web.events(action=audit_service.SETTINGS_UPDATE)
    assert set(event) == FIELDS
    assert (event["actor"], event["resource_type"], event["resource_id"]) == (
        web.user,
        "settings",
        None,
    )
    if "x-request-id" in r.headers:  # middleware request id gắn vào contextvars của structlog
        assert event["request_id"] == r.headers["x-request-id"]
    changes = event["changes"]
    assert set(changes) == {"poll_interval_seconds", "proxies", "serp_api_key"}
    assert changes["poll_interval_seconds"] == {
        "before": web.settings.poll_interval_seconds,
        "after": 120,
    }
    assert changes["serp_api_key"] == {"before": None, "after": app_settings_service.MASK}
    assert changes["proxies"] == {
        "before": [],
        "after": [f"http://u1:{app_settings_service.PROXY_MASK}@10.0.0.1:8080"],
    }
    assert event["metadata"] is None
    async with web.res.sessionmaker() as s:
        rows = (await s.execute(select(AuditEvent.changes, AuditEvent.meta))).all()
    dump = json.dumps([list(row) for row in rows], ensure_ascii=False)
    assert secret not in dump
    assert proxy_password not in dump

    # Lưu lại đúng giá trị đang lưu / ô bí mật để nguyên: không có thay đổi, không ghi nhật ký.
    r = await web.client.put(
        "/api/settings",
        json={"values": {"poll_interval_seconds": 120, "serp_api_key": app_settings_service.MASK}},
    )
    assert r.status_code == 200, r.text
    assert len(await web.events(action=audit_service.SETTINGS_UPDATE)) == 1

    # Chỉ về env / mặc định: `settings.reset`.
    assert (await web.client.delete("/api/settings/serp_api_key")).status_code == 200
    [reset] = await web.events(action=audit_service.SETTINGS_RESET)
    assert reset["changes"] == {
        "serp_api_key": {"before": app_settings_service.MASK, "after": None}
    }
    assert reset["metadata"] == {"reset": ["serp_api_key"]}
    # Về mặc định một key đã không có dòng: không đổi gì.
    assert (await web.client.delete("/api/settings/serp_api_key")).status_code == 200
    assert len(await web.events(action=audit_service.SETTINGS_RESET)) == 1

    # Vừa sửa vừa về mặc định: `settings.update`, `metadata.reset` liệt kê key về mặc định.
    r = await web.client.put(
        "/api/settings",
        json={"values": {"poll_interval_seconds": None, "llm_model": f"model-{web.tag}"}},
    )
    assert r.status_code == 200, r.text
    latest = (await web.events(action=audit_service.SETTINGS_UPDATE))[0]
    assert latest["changes"]["poll_interval_seconds"] == {
        "before": 120,
        "after": web.settings.poll_interval_seconds,
    }
    assert latest["changes"]["llm_model"]["after"] == f"model-{web.tag}"
    assert latest["metadata"] == {"reset": ["poll_interval_seconds"]}

    # Lưu không qua request (CLI, cron): người thao tác `system`.
    model = f"system-{web.tag}"
    await app_settings_service.save(
        web.res.sessionmaker,
        web.res.redis,
        web.settings.redis_prefix,
        web.settings,
        {"llm_model": model},
        user=None,
    )
    async with web.res.sessionmaker() as s:
        system = (
            await s.scalars(
                select(AuditEvent)
                .where(AuditEvent.actor == audit_service.SYSTEM)
                .order_by(AuditEvent.id.desc())
                .limit(1)
            )
        ).one()
    assert system.changes is not None
    assert system.changes["llm_model"]["after"] == model


async def test_record_joins_transaction_and_record_safely_never_raises(web: Web) -> None:
    with structlog.contextvars.bound_contextvars(request_id=f"req-{web.tag}"):
        async with web.res.sessionmaker.begin() as s:
            await audit_service.record(
                s,
                action=audit_service.COMPANY_BUILD,
                actor=web.user,
                resource_type="company",
                resource_id=web.company_id,
                metadata={"queued": True, "bo_qua": None},
            )

    # Transaction của bên gọi rollback thì dòng nhật ký cũng không còn.
    async def fail_after_record() -> None:
        async with web.res.sessionmaker.begin() as s:
            await audit_service.record(s, action=audit_service.COMPANY_POLL, actor=web.user)
            raise RuntimeError("rollback")

    with pytest.raises(RuntimeError, match="rollback"):
        await fail_after_record()
    # Ghi lỗi (Postgres không nhận ký tự NUL trong text): chỉ log cảnh báo, không ném lỗi.
    await audit_service.record_safely(
        web.res.sessionmaker, action=audit_service.COMPANY_POLL, actor="x\x00y"
    )

    items = await web.events()
    assert [(e["action"], e["request_id"], e["metadata"]) for e in items] == [
        (audit_service.COMPANY_BUILD, f"req-{web.tag}", {"queued": True})
    ]
    assert items[0]["resource_id"] == str(web.company_id)
    assert items[0]["changes"] is None


async def test_operator_actions_are_audited(web: Web) -> None:
    cid, jid = str(web.company_id), str(web.job_id)
    assert (await web.client.post(f"/api/companies/{cid}/build")).json() == {"queued": True}
    assert (await web.client.post(f"/api/companies/{cid}/poll")).json() == {"queued": True}
    r = await web.client.post("/api/enqueue", json={"company_ids": [cid]})
    assert r.status_code == 200, r.text
    enqueued = r.json()
    csv = f"name,website\nAudit mới {web.tag},auditmoi{web.tag}.vn\n".encode()
    r = await web.client.post(
        "/api/companies/import", files={"file": ("companies.csv", csv, "text/csv")}
    )
    assert r.json()["created"] == 1
    review = f"/api/jobs/{jid}/review"
    assert (
        await web.client.post(review, json={"correct": False, "note": " sai "})
    ).status_code == 200
    assert (await web.client.post(review, json={"correct": True, "note": "sai"})).status_code == 200
    assert (await web.client.delete(review)).status_code == 200
    assert (await web.client.delete(review)).status_code == 200  # không còn gì để xoá: không ghi
    assert (await web.client.post("/api/boards/topcv/index")).json()["queued"] is True
    assert (await web.client.post("/api/maintenance")).json() == {"queued": True}
    keys = registry_service.Keys(web.settings.redis_prefix)
    worker = registry_service.WorkerStatus(
        worker_id=f"w-{web.tag}", hostname="may-1", pool="default"
    )
    await registry_service.save_worker(web.res.redis, keys, worker, ttl=30)
    assert (await web.client.post(f"/api/workers/w-{web.tag}/drain")).status_code == 200
    assert (await web.client.delete(f"/api/workers/w-{web.tag}/drain")).status_code == 200
    # Thao tác bị từ chối không ghi nhật ký.
    assert (await web.client.post("/api/boards/khong-co/index")).status_code == 404
    assert (await web.client.post("/api/enqueue", json={})).status_code == 400
    evil = await web.client.post("/api/maintenance", headers={"Origin": "https://evil.example"})
    assert evil.status_code == 403

    events = list(reversed(await web.events()))
    assert [e["action"] for e in events] == [
        audit_service.COMPANY_BUILD,
        audit_service.COMPANY_POLL,
        audit_service.COMPANIES_ENQUEUE,
        audit_service.COMPANIES_IMPORT,
        audit_service.JOB_REVIEW,
        audit_service.JOB_REVIEW,
        audit_service.JOB_UNREVIEW,
        audit_service.BOARD_INDEX,
        audit_service.MAINTENANCE_RUN,
        audit_service.WORKER_DRAIN,
        audit_service.WORKER_UNDRAIN,
    ]
    assert {e["action"] for e in events} <= set(audit_service.ACTIONS)
    assert {e["actor"] for e in events} == {web.user}
    build, poll, enqueue, imported, first, second, unreview, board, maint, drain, undrain = events
    assert (build["resource_type"], build["resource_id"]) == ("company", cid)
    assert build["metadata"] == {"queued": True, "trigger": "manual"}
    assert (poll["resource_id"], poll["metadata"]["queued"]) == (cid, True)
    assert (enqueue["resource_type"], enqueue["resource_id"]) == ("company", cid)
    assert enqueue["metadata"] == {**enqueued, "all": False, "company_ids": [cid]}
    assert imported["resource_type"] is None
    assert imported["metadata"]["filename"] == "companies.csv"
    assert (imported["metadata"]["rows"], imported["metadata"]["created"]) == (1, 1)
    assert imported["metadata"]["enqueue"] is False
    assert (first["resource_type"], first["resource_id"]) == ("job", jid)
    assert first["changes"] == {
        "correct": {"before": None, "after": False},
        "note": {"before": None, "after": "sai"},
    }
    assert second["changes"] == {"correct": {"before": False, "after": True}}
    assert unreview["changes"] == {
        "correct": {"before": True, "after": None},
        "note": {"before": "sai", "after": None},
    }
    assert (board["resource_type"], board["resource_id"], board["metadata"]) == (
        "board",
        "topcv",
        {"queued": True},
    )
    assert maint["metadata"] == {"queued": True}
    assert (drain["resource_type"], drain["resource_id"]) == ("worker", f"w-{web.tag}")
    assert drain["metadata"] == {"hostname": "may-1", "pool": "default"}
    assert undrain["resource_id"] == f"w-{web.tag}"

    # Lịch sử của một doanh nghiệp: lọc theo tài nguyên.
    history = await web.events(resource_type="company", resource_id=cid)
    assert [e["action"] for e in history] == [
        audit_service.COMPANIES_ENQUEUE,
        audit_service.COMPANY_POLL,
        audit_service.COMPANY_BUILD,
    ]


async def test_login_logout_export_are_audited_with_source_and_labels(web: Web) -> None:
    ua = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/141.0 Safari/537.36"}
    bad = await web.client.post(
        "/api/auth/login", json={"username": web.user, "password": "sai"}, headers=ua
    )
    assert bad.status_code == 401
    ok = await web.client.post(
        "/api/auth/login", json={"username": web.user, "password": "mat-khau"}, headers=ua
    )
    assert ok.status_code == 200, ok.text
    token = ok.json()["access_token"]
    out = await web.client.post(
        "/api/auth/logout", headers={"Authorization": f"Bearer {token}"} | ua
    )
    assert out.json() == {"ok": True}
    export = await web.client.get(
        "/api/jobs/export", params={"fmt": "jsonl", "company_id": str(web.company_id)}
    )
    assert export.status_code == 200
    assert (await web.client.post(f"/api/companies/{web.company_id}/poll")).status_code == 200
    review = f"/api/jobs/{web.job_id}/review"
    assert (await web.client.post(review, json={"correct": True})).status_code == 200

    events = list(reversed(await web.events()))
    assert [e["action"] for e in events] == [
        audit_service.AUTH_LOGIN_FAILED,
        audit_service.AUTH_LOGIN,
        audit_service.AUTH_LOGOUT,
        audit_service.JOBS_EXPORT,
        audit_service.COMPANY_POLL,
        audit_service.JOB_REVIEW,
    ]
    failed, login, logout, exported, poll, reviewed = events
    # Nguồn request: IP của client (ASGITransport: 127.0.0.1) và User-Agent đã gửi.
    assert {e["ip"] for e in events} == {"127.0.0.1"}
    assert login["user_agent"] == ua["User-Agent"]
    assert failed["metadata"] == {"failures": 1, "locked": False}
    assert "mat-khau" not in json.dumps(events, ensure_ascii=False)
    assert login["resource_type"] == logout["resource_type"] == "session"
    assert login["resource_id"] == logout["resource_id"]  # cùng jti của phiên
    assert exported["metadata"] == {
        "fmt": "jsonl",
        "filters": {"company_id": str(web.company_id)},
    }
    # Tên hiện của đối tượng: tra lúc đọc.
    assert poll["resource_label"] == f"Công ty Audit {web.tag}"
    assert reviewed["resource_label"] == f"Kỹ sư {web.tag} · Công ty Audit {web.tag}"
    assert login["resource_label"] is None


async def test_list_filters_and_keyset_pagination(web: Web) -> None:
    other = f"other-{web.tag}"
    async with web.res.sessionmaker.begin() as s:
        # Cùng một transaction: `occurred_at` (now()) như nhau, thứ tự chỉ còn ở id (UUIDv7).
        for i in range(5):
            await audit_service.record(
                s,
                action=audit_service.COMPANY_POLL,
                actor=web.user,
                resource_type="company",
                resource_id=web.company_id,
                metadata={"i": i},
            )
        await audit_service.record(
            s,
            action=audit_service.BOARD_INDEX,
            actor=web.user,
            resource_type="board",
            resource_id="topcv",
        )
        await audit_service.record(
            s,
            action=audit_service.COMPANY_POLL,
            actor=other,
            resource_type="company",
            resource_id=web.company_id,
        )

    seen: list[dict[str, Any]] = []
    before: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {
            "actor": web.user,
            "action": audit_service.COMPANY_POLL,
            "limit": 2,
        }
        if before is not None:
            params["before"] = before
        r = await web.client.get("/api/audit", params=params)
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body) == {"items", "next"}
        seen += body["items"]
        pages += 1
        before = body["next"]
        if before is None:
            break
        assert before == body["items"][-1]["id"]
    assert pages == 3
    assert [e["metadata"]["i"] for e in seen] == [4, 3, 2, 1, 0]
    ids = [e["id"] for e in seen]
    assert ids == sorted(ids, reverse=True)
    assert len(set(ids)) == 5
    assert set(seen[0]) == FIELDS
    assert datetime.fromisoformat(seen[0]["occurred_at"]).tzinfo is not None

    # Trang vừa đủ: không còn trang sau.
    body = (
        await web.client.get(
            "/api/audit",
            params={"actor": web.user, "action": audit_service.COMPANY_POLL, "limit": 5},
        )
    ).json()
    assert (len(body["items"]), body["next"]) == (5, None)

    company = await web.events(actor="", resource_type="company", resource_id=str(web.company_id))
    assert len(company) == 6
    assert {e["actor"] for e in company} == {web.user, other}
    boards = await web.events(resource_type="board")
    assert [(e["action"], e["resource_id"]) for e in boards] == [
        (audit_service.BOARD_INDEX, "topcv")
    ]
    assert len(await web.events(actor=other)) == 1

    for bad in ({"limit": 0}, {"limit": 201}, {"before": "khong-phai-uuid"}):
        assert (await web.client.get("/api/audit", params=bad)).status_code == 422

    actions = (await web.client.get("/api/audit/actions")).json()
    assert actions == sorted(audit_service.ACTIONS)


async def test_pending_events_from_gateway_reach_the_table(web: Web) -> None:
    """Gateway (không nối Postgres) để dòng `worker.joined` trên Redis; scheduler ghi vào bảng với
    thời điểm / IP lúc xảy ra. Lọc theo khoá tham gia thấy cả worker đã dùng khoá."""
    redis, prefix = web.res.redis, web.settings.redis_prefix
    key = audit_service.pending_key(prefix)
    join_id, worker_id = f"j{web.tag}", f"w-{web.tag}:1"
    await audit_service.record_safely(
        web.res.sessionmaker,
        action=audit_service.WORKER_JOIN_KEY,
        actor=web.user,
        resource_type="join_key",
        resource_id=join_id,
        metadata={"ttl_seconds": 900},
    )
    queued_at = datetime.now(UTC)
    assert await audit_service.queue_pending(
        redis,
        prefix,
        action=audit_service.WORKER_JOINED,
        actor=audit_service.WORKER,
        resource_type="worker",
        resource_id=worker_id,
        metadata={"join_id": join_id, "hostname": "may-1", "pool": "default", "version": None},
        ip="203.0.113.7",
    )
    await redis.rpush(key, "khong-phai-json")  # pyright: ignore[reportGeneralTypeIssues]
    before = await redis.lrange(key, 0, -1)  # pyright: ignore[reportGeneralTypeIssues]

    class DownSessionmaker:
        def begin(self) -> Any:
            raise ConnectionRefusedError("postgres down")

    # Postgres lỗi: lô trở về hàng chờ, nguyên thứ tự.
    with pytest.raises(ConnectionRefusedError):
        await audit_service.flush_pending(redis, prefix, cast(Any, DownSessionmaker()))
    assert await redis.lrange(key, 0, -1) == before  # pyright: ignore[reportGeneralTypeIssues]

    # Mục hỏng bị bỏ; ghi lại cùng dòng (trùng id) không thành hai dòng.
    assert await audit_service.flush_pending(redis, prefix, web.res.sessionmaker, batch=1) == 1
    assert await redis.llen(key) == 0  # pyright: ignore[reportGeneralTypeIssues]
    await redis.rpush(key, before[0])  # pyright: ignore[reportGeneralTypeIssues]
    assert await audit_service.flush_pending(redis, prefix, web.res.sessionmaker) == 0

    joined = await web.events(actor="", resource_type="worker", resource_id=worker_id)
    assert len(joined) == 1
    event = joined[0]
    assert {k: event[k] for k in ("action", "actor", "ip", "user_agent", "request_id")} == {
        "action": audit_service.WORKER_JOINED,
        "actor": "worker",
        "ip": "203.0.113.7",
        "user_agent": None,
        "request_id": None,
    }
    assert event["metadata"] == {"join_id": join_id, "hostname": "may-1", "pool": "default"}
    # Thời điểm lúc gateway đẩy vào hàng chờ, không phải lúc scheduler ghi.
    occurred = datetime.fromisoformat(event["occurred_at"])
    assert abs(occurred - queued_at) < timedelta(seconds=2)

    # Lọc theo khoá: dòng tạo khoá kèm dòng worker đã dùng khoá (mới nhất trước).
    by_key = await web.events(actor="", resource_type="join_key", resource_id=join_id)
    assert [(e["action"], e["resource_id"]) for e in by_key] == [
        (audit_service.WORKER_JOINED, worker_id),
        (audit_service.WORKER_JOIN_KEY, join_id),
    ]
    assert await web.events(actor="", resource_type="join_key", resource_id=f"x{join_id}") == []

    # Tên máy do worker gửi có NUL (Postgres không nhận): bỏ ký tự, không kẹt hàng chờ.
    await audit_service.queue_pending(
        redis,
        prefix,
        action=audit_service.WORKER_JOINED,
        actor=audit_service.WORKER,
        resource_type="worker",
        resource_id=f"{worker_id}-2",
        metadata={"join_id": f"{join_id}-2", "hostname": "may\x00-2"},
    )
    assert await audit_service.flush_pending(redis, prefix, web.res.sessionmaker) == 1
    second = await web.events(actor="", resource_type="worker", resource_id=f"{worker_id}-2")
    assert second[0]["metadata"] == {"join_id": f"{join_id}-2", "hostname": "may-2"}


async def test_maintenance_does_not_prune_audit_events(web: Web) -> None:
    old = datetime.now(UTC) - timedelta(days=3650)
    async with web.res.sessionmaker.begin() as s:
        event = await audit_service.record(s, action=audit_service.MAINTENANCE_RUN, actor=web.user)
        await s.execute(update(AuditEvent).where(AuditEvent.id == event.id).values(occurred_at=old))
        log = Log(ts=old, level="info", event=f"audit-test-{web.tag}")
        s.add(log)
        await s.flush()
        event_id, log_id = event.id, log.id

    report = await maintenance_service.run_maintenance(web.res, compression=False)
    assert report.skipped is None
    assert report.deleted["logs"] >= 1
    assert "audit_events" not in report.deleted
    async with web.res.sessionmaker() as s:
        kept = await s.scalar(select(AuditEvent.occurred_at).where(AuditEvent.id == event_id))
        assert await s.scalar(select(Log.id).where(Log.id == log_id)) is None
    assert kept == old
