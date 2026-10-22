"""API tích hợp cho hệ thống ngoài trên Postgres + Redis thật: quản lý client / khoá (trang Tích
hợp), đẩy doanh nghiệp (từng cái, lô, CSV), tắt / bật theo dõi, snapshot + feed, long-poll,
quyền theo khoá."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import select

from jobcrawler.bootstrap.app import create_app
from jobcrawler.common.config import Settings
from jobcrawler.database.models import CompanySchedule
from jobcrawler.modules.integration import relay_service
from jobcrawler.pipeline import dedup, store
from jobcrawler.pipeline.verify import Verdict
from jobcrawler.services.redis.queues import drop_queue
from jobcrawler.services.resources import Resources

from ..jobs_fixtures import NOW, normalized, raw_job

pytestmark = pytest.mark.integration

V1 = "/api/integration/v1"


@dataclass
class Web:
    ui: httpx.AsyncClient
    app: Any
    res: Resources
    tag: str

    def partner(self, key: str) -> httpx.AsyncClient:
        """Client của hệ thống ngoài: chỉ khoá API, không cookie / Origin."""
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://api.test",
            headers={"Authorization": f"Bearer {key}"},
        )

    async def new_client(self, **body: Any) -> tuple[str, str]:
        r = await self.ui.post(
            "/api/api-clients", json={"name": f"đối tác {uuid4().hex[:6]}"} | body
        )
        assert r.status_code == 200, r.text
        client_id = r.json()["id"]
        r = await self.ui.post(f"/api/api-clients/{client_id}/keys")
        assert r.status_code == 200, r.text
        return client_id, r.json()["key"]


@contextlib.asynccontextmanager
async def open_web(integration_settings: Settings) -> AsyncIterator[Web]:
    tag = uuid4().hex[:8]
    settings = integration_settings.model_copy(
        update={
            "redis_prefix": f"jct{tag}",
            "build_queue_name": f"jobcrawler-test-build-{tag}",
            "poll_queue_name": f"jobcrawler-test-poll-{tag}",
            "board_queue_name": f"jobcrawler-test-boards-{tag}",
            "llm_base_url": None,
            "serp_api_key": None,
            "ui_password": SecretStr("mat-khau"),
        }
    )
    app = create_app(settings)
    try:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://ui.test",
                headers={
                    "Authorization": f"Bearer {app.state.token_auth.issue().token}",
                    "Origin": "http://ui.test",
                },
            ) as ui,
        ):
            yield Web(ui, app, app.state.res, tag)
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


@pytest.fixture
async def web(integration_settings: Settings) -> AsyncIterator[Web]:
    async with open_web(integration_settings) as w:
        yield w


async def state_of(web: Web, company_id: str) -> str | None:
    async with web.res.sessionmaker() as s:
        return await s.scalar(
            select(CompanySchedule.state).where(CompanySchedule.company_id == UUID(company_id))
        )


async def save_job(web: Web, company_id: str, code: str, title: str = "Kế toán tổng hợp") -> None:
    job = normalized(
        raw_job(external_id=code, url=f"https://careers.acme.vn/jobs/{code}", title=title),
        board=False,
    )
    async with web.res.sessionmaker.begin() as s:
        await store.save_company_jobs(
            s,
            company_id=UUID(company_id),
            company_name="ACME",
            jobs=[job],
            verdicts=[Verdict(accepted=True, confidence=0.9, reasons=["TEST"])],
            clusters=dedup.cluster_jobs([job]),
            now=NOW + timedelta(minutes=1),
            complete=False,
        )


async def test_push_companies_single_batch_csv_and_pause(web: Web) -> None:
    _, key = await web.new_client()
    name = f"Công ty Tích Hợp {web.tag}"
    async with web.partner(key) as api:
        r = await api.put(
            f"{V1}/companies/C-1",
            json={"name": name, "website": f"{web.tag}.vn", "tax_code": "12ab"},
        )
        assert r.status_code == 200, r.text
        first = r.json()
        assert first["result"] == "created"
        assert first["warnings"] == ["MST '12ab' không hợp lệ, bỏ qua"]
        company_id = first["company_id"]

        again = (await api.put(f"{V1}/companies/C-1", json={"name": name})).json()
        assert (again["result"], again["company_id"]) == ("unchanged", company_id)
        changed = await api.put(
            f"{V1}/companies/C-1",
            json={"name": name, "careers_url": f"https://{web.tag}.vn/tuyen-dung"},
        )
        assert changed.json()["result"] == "updated"

        batch = await api.post(
            f"{V1}/companies/batch",
            json={
                "items": [
                    {"external_id": "C-2", "name": f"Doanh nghiệp khác {web.tag}"},
                    {"external_id": "C-1b", "name": name},  # cùng doanh nghiệp, mã thứ hai
                    {"external_id": "có dấu cách", "name": "ABC"},
                    {"external_id": "C-2", "name": f"Doanh nghiệp khác {web.tag}", "note": "sau"},
                ]
            },
        )
        assert batch.status_code == 200, batch.text
        body = batch.json()
        assert [(x["external_id"], x["result"]) for x in body["results"]] == [
            ("C-2", "unchanged"),
            ("C-1b", "linked"),
            ("có dấu cách", "error"),
            ("C-2", "created"),
        ]
        assert body["results"][1]["company_id"] == company_id
        assert (body["created"], body["linked"], body["errors"]) == (1, 1, 1)

        # Tắt một mã: doanh nghiệp còn mã khác đang theo dõi nên vẫn thu thập.
        await api.put(f"{V1}/companies/C-1", json={"name": name, "active": False})
        assert await state_of(web, company_id) != "paused"
        csv = f"external_id,name,active\nC-1b,{name},0\nC-3,Thêm bằng CSV {web.tag},\n"
        r = await api.post(
            f"{V1}/companies/import", files={"file": ("ds.csv", csv.encode(), "text/csv")}
        )
        assert r.status_code == 200, r.text
        assert [x["result"] for x in r.json()["results"]] == ["updated", "created"]
        assert await state_of(web, company_id) == "paused"
        status = (await api.get(f"{V1}/companies/C-1")).json()
        assert (status["crawl_state"], status["active"]) == ("paused", False)
        assert status["careers_url"] == f"https://{web.tag}.vn/tuyen-dung"
        refresh = (await api.post(f"{V1}/companies/C-1/refresh")).json()
        assert refresh["queued"] is False

        await api.put(f"{V1}/companies/C-1", json={"name": name})
        assert await state_of(web, company_id) == "new"

        listed = (
            await api.get(f"{V1}/companies", params={"sortBy": "external_id", "sortOrder": "ASC"})
        ).json()
        assert [c["external_id"] for c in listed["data"]] == ["C-1", "C-1b", "C-2", "C-3"]
        assert (await api.get(f"{V1}/companies/khong-co")).status_code == 404

    detail = (await web.ui.get(f"/api/companies/{company_id}")).json()
    assert sorted(r["external_id"] for r in detail["refs"]) == ["C-1", "C-1b"]
    assert detail["input_careers_url"] == f"https://{web.tag}.vn/tuyen-dung"
    audit = (await web.ui.get("/api/audit", params={"action": "integration.companies"})).json()
    assert audit["items"]
    assert audit["items"][0]["actor"].startswith("api:đối tác")


async def test_snapshot_then_feed_with_long_poll(web: Web) -> None:
    _, key = await web.new_client(include_description=False)
    async with web.partner(key) as api:
        company_id = (
            await api.put(f"{V1}/companies/S-1", json={"name": f"Snapshot {web.tag}"})
        ).json()["company_id"]
        await relay_service.sequence_pending(web.res)
        snap = (await api.get(f"{V1}/jobs")).json()
        assert snap["data"] == []
        cursor = snap["feed_cursor"]

        await save_job(web, company_id, f"a-{web.tag}")
        await relay_service.sequence_pending(web.res)
        page = (await api.get(f"{V1}/jobs/changes", params={"cursor": cursor})).json()
        [event] = page["events"]
        assert event["type"] == "created"
        assert event["job"]["company_external_ids"] == ["S-1"]
        assert event["job"]["description_md"] == ""
        assert not page["has_more"]
        cursor = page["next_cursor"]

        by_company = (await api.get(f"{V1}/companies/S-1/jobs")).json()
        assert [j["id"] for j in by_company["data"]] == [event["job_id"]]
        filtered = (await api.get(f"{V1}/jobs", params={"external_id": ["S-1", "khac"]})).json()
        assert [j["id"] for j in filtered["data"]] == [event["job_id"]]

        async def later() -> None:
            await asyncio.sleep(0.5)
            await save_job(web, company_id, f"b-{web.tag}", title="Lập trình viên Java")
            await relay_service.sequence_pending(web.res)

        started = time.monotonic()
        waited, _ = await asyncio.gather(
            api.get(f"{V1}/jobs/changes", params={"cursor": cursor, "wait": 10}), later()
        )
        assert time.monotonic() - started < 8
        assert [e["type"] for e in waited.json()["events"]] == ["created"]

        gone = await api.get(f"{V1}/jobs/changes", params={"cursor": "999999999999"})
        assert gone.status_code == 410
        assert gone.json()["detail"].startswith("resync_required")
        assert (await api.get(f"{V1}/jobs/changes", params={"cursor": "abc"})).status_code == 400

        spec = (await api.get(f"{V1}/openapi.json")).json()
        assert spec["paths"]
        assert all(p.startswith(V1) for p in spec["paths"])


async def test_keys_scopes_and_revocation(web: Web) -> None:
    client_id, key = await web.new_client(scopes=["jobs:read"])
    async with web.partner(key) as api:
        assert (await api.get(f"{V1}/jobs")).status_code == 200
        r = await api.put(f"{V1}/companies/X", json={"name": "Không được ghi"})
        assert r.status_code == 403
        # Khoá API không mở được API của giao diện.
        assert (await api.get("/api/companies")).status_code == 401
    # JWT giao diện không mở được API tích hợp.
    assert (await web.ui.get(f"{V1}/jobs")).status_code == 401

    detail = (await web.ui.get(f"/api/api-clients/{client_id}")).json()
    [key_row] = detail["keys"]
    assert key.startswith("jca_" + key_row["prefix"])
    assert key not in str(detail)
    r = await web.ui.delete(f"/api/api-clients/{client_id}/keys/{key_row['id']}")
    assert r.status_code == 200
    async with web.partner(key) as api:
        assert (await api.get(f"{V1}/jobs")).status_code == 401

    r = await web.ui.patch(f"/api/api-clients/{client_id}", json={"all_companies": True})
    assert r.json()["all_companies"] is True
    listed = (await web.ui.get("/api/api-clients", params={"search": detail["name"]})).json()
    assert [c["id"] for c in listed["data"]] == [client_id]
    assert (await web.ui.delete(f"/api/api-clients/{client_id}")).status_code == 200
    assert (await web.ui.get(f"/api/api-clients/{client_id}")).status_code == 404
