"""Webhook ký HMAC tới hệ thống ngoài trên Postgres + Redis thật và một bên nhận cục bộ: lỗi thì
giữ cursor và gửi lại đúng lô, 2xx thì tiến cursor, hai relay chồng nhau không làm cursor lùi,
cursor đã dọn thì nhảy và báo lỗi, relay đánh thức task giao khi có seq mới."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from jobcrawler.common.config import Settings
from jobcrawler.common.schemas import JobEventType
from jobcrawler.database.models import ApiClient, Company, CompanyRef, JobEventCounter
from jobcrawler.modules.integration import feed_service, relay_service, webhook_service
from jobcrawler.pipeline import store
from jobcrawler.services.resources import Resources, open_resources

from .servers import Receiver, serve_receiver
from .test_integration_api_db import open_web

pytestmark = pytest.mark.integration

SECRET = "whsec_test"


async def setup_client(res: Resources, url: str, *, enabled: bool = True) -> tuple[UUID, UUID]:
    """(client, doanh nghiệp đã gắn mã) với webhook trỏ tới `url`, cursor ở seq mới nhất."""
    await relay_service.sequence_pending(res)
    async with res.sessionmaker.begin() as s:
        company = Company(input_name=f"Webhook {uuid4().hex[:8]}")
        client = ApiClient(
            name=f"wh-{uuid4().hex[:8]}",
            webhook_url=webhook_service.seal(webhook_service.URL_FIELD, url),
            webhook_secret=webhook_service.seal(webhook_service.SECRET_FIELD, SECRET),
        )
        s.add_all([company, client])
        await s.flush()
        s.add(CompanyRef(client_id=client.id, external_id="W-1", company_id=company.id))
        if enabled:
            client.webhook_enabled = True
            await webhook_service.start_cursor(s, client)
        return client.id, company.id


async def emit(res: Resources, company_id: UUID, n: int = 1) -> list[UUID]:
    jobs = [uuid4() for _ in range(n)]
    async with res.sessionmaker.begin() as s:
        await store.emit_job_events(s, [(j, company_id, JobEventType.CREATED) for j in jobs])
    await relay_service.sequence_pending(res)
    return jobs


async def target_of(res: Resources, client_id: UUID) -> webhook_service.Target:
    [target] = [t for t in await webhook_service.targets(res) if t.client_id == client_id]
    return target


async def client_row(res: Resources, client_id: UUID) -> ApiClient:
    async with res.sessionmaker() as s:
        row = await s.get(ApiClient, client_id)
        assert row is not None
        return row


def verified(receiver: Receiver, secret: str = SECRET) -> list[dict[str, Any]]:
    """Body của các request bên nhận đã nhận, sau khi kiểm chữ ký."""
    out = []
    for headers, body in receiver.requests:
        ts = headers["x-jc-timestamp"]
        expected = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
        assert headers["x-jc-signature"] == f"v1={expected}"
        assert headers["content-type"] == "application/json"
        payload = json.loads(body)
        assert payload["delivery_id"] == headers["x-jc-delivery"]
        out.append(payload)
    return out


async def test_failure_keeps_cursor_then_success_advances(integration_settings: Settings) -> None:
    async with open_resources(integration_settings, db_logs=False) as res:
        with serve_receiver([500]) as receiver:
            client_id, company_id = await setup_client(res, receiver.url)
            start = (await client_row(res, client_id)).webhook_cursor
            jobs = await emit(res, company_id, 3)
            target = await target_of(res, client_id)

            failed = await webhook_service.deliver(res, target)
            assert failed.error == "HTTP 500"
            row = await client_row(res, client_id)
            assert row.webhook_cursor == start
            assert row.webhook_failing_since is not None
            assert row.webhook_last_error == "HTTP 500"

            ok = await webhook_service.deliver(res, target)
            assert (ok.error, ok.sent) == (None, 3)
            row = await client_row(res, client_id)
            async with res.sessionmaker() as s:
                head = await feed_service.head(s)
            assert row.webhook_cursor == head
            assert row.webhook_failing_since is None
            assert row.webhook_last_ok_at is not None

            first, second = verified(receiver)
            assert first["events"] == second["events"]  # gửi lại đúng lô đó
            assert [e["job_id"] for e in second["events"]] == [str(j) for j in jobs]
            assert {e["type"] for e in second["events"]} == {"created"}
            assert second["cursor"] == str(head)
            # Đã giao hết: lần sau không gửi gì.
            assert (await webhook_service.deliver(res, target)).sent == 0
            assert len(receiver.requests) == 2


async def test_overlapping_relays_never_move_the_cursor_back(
    integration_settings: Settings,
) -> None:
    async with open_resources(integration_settings, db_logs=False) as res:
        with serve_receiver() as receiver:
            client_id, company_id = await setup_client(res, receiver.url)
            await emit(res, company_id, 5)
            target = await target_of(res, client_id)
            await asyncio.gather(*(webhook_service.deliver(res, target) for _ in range(3)))
            async with res.sessionmaker() as s:
                head = await feed_service.head(s)
            assert (await client_row(res, client_id)).webhook_cursor == head
            delivered = {e["seq"] for p in verified(receiver) for e in p["events"]}
            assert len(delivered) == 5  # có thể trùng, không thiếu


async def test_pruned_cursor_jumps_and_reports(integration_settings: Settings) -> None:
    async with open_resources(integration_settings, db_logs=False) as res:
        with serve_receiver() as receiver:
            client_id, company_id = await setup_client(res, receiver.url)
            await emit(res, company_id)
            async with res.sessionmaker.begin() as s:
                _, last = await feed_service.bounds(s)
                await s.execute(update(JobEventCounter).values(pruned=last))
                await s.execute(
                    update(ApiClient).where(ApiClient.id == client_id).values(webhook_cursor=1)
                )
            target = await target_of(res, client_id)
            await webhook_service.deliver(res, target)
            row = await client_row(res, client_id)
            assert row.webhook_cursor == last
            assert row.webhook_last_error is not None
            assert "đồng bộ lại" in row.webhook_last_error
            assert receiver.requests == []


async def test_relay_wakes_delivery_tasks(integration_settings: Settings) -> None:
    settings = integration_settings.model_copy(
        update={"redis_prefix": f"jc-wh-{uuid4().hex[:6]}", "relay_interval_seconds": 0.2}
    )
    async with open_resources(settings, db_logs=False) as res:
        with serve_receiver() as receiver:
            _, company_id = await setup_client(res, receiver.url)
            stop = asyncio.Event()
            relay = asyncio.create_task(relay_service.run_relay(res, stop))
            try:
                await asyncio.sleep(0.5)  # relay giữ lease, đọc danh sách client
                async with res.sessionmaker.begin() as s:
                    job = uuid4()
                    await store.emit_job_events(s, [(job, company_id, JobEventType.CREATED)])
                for _ in range(50):
                    if receiver.requests:
                        break
                    await asyncio.sleep(0.1)
            finally:
                stop.set()
                await relay
            [payload] = verified(receiver)
            assert [e["job_id"] for e in payload["events"]] == [str(job)]


async def test_admin_webhook_settings_secret_and_test(integration_settings: Settings) -> None:
    async with open_web(integration_settings) as web:
        with serve_receiver() as receiver:
            client_id = (
                await web.ui.post("/api/api-clients", json={"name": f"wh {web.tag}"})
            ).json()["id"]
            r = await web.ui.patch(
                f"/api/api-clients/{client_id}",
                json={"webhook_url": receiver.url, "webhook_enabled": True},
            )
            assert r.status_code == 400  # chưa có secret
            r = await web.ui.patch(
                f"/api/api-clients/{client_id}", json={"webhook_url": "ftp://sai"}
            )
            assert r.status_code == 400
            r = await web.ui.patch(
                f"/api/api-clients/{client_id}", json={"webhook_url": receiver.url}
            )
            assert r.json()["webhook"]["url"].endswith("/hooks/jobs?…")
            secret = (await web.ui.post(f"/api/api-clients/{client_id}/webhook/secret")).json()[
                "secret"
            ]
            r = await web.ui.patch(f"/api/api-clients/{client_id}", json={"webhook_enabled": True})
            webhook = r.json()["webhook"]
            assert (webhook["enabled"], webhook["secret_set"], webhook["lag"]) == (True, True, 0)
            assert secret not in r.text
            assert "bi-mat" not in r.text  # token trong query của URL không bị lộ
            test = (await web.ui.post(f"/api/api-clients/{client_id}/webhook/test")).json()
            assert (test["ok"], test["status"]) == (True, 200)
            [payload] = verified(receiver, secret)
            assert payload["test"] is True
            async with web.res.sessionmaker() as s:
                stored = await s.scalar(
                    select(ApiClient.webhook_url).where(ApiClient.id == UUID(client_id))
                )
            assert stored is not None
            assert "bi-mat" not in stored  # lưu mã hoá
