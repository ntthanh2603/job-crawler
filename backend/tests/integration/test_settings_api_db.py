"""API trang Cài đặt và trang Workers trên Postgres + Redis thật."""

from __future__ import annotations

import base64
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import delete, select

from jobcrawler.config import Settings
from jobcrawler.db.models import AppSetting
from jobcrawler.gateway import registry
from jobcrawler.queues import drop_queue
from jobcrawler.resources import Resources
from jobcrawler.services import app_settings
from jobcrawler.web.app import create_app

pytestmark = pytest.mark.integration

AUTH = {"Authorization": "Basic " + base64.b64encode(b"admin:mat-khau").decode()}
ORIGIN = {"Origin": "http://ui.test"}


@dataclass
class Web:
    client: httpx.AsyncClient
    settings: Settings
    res: Resources

    @property
    def prefix(self) -> str:
        return self.settings.redis_prefix


def by_key(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["key"]: item for g in payload["groups"] for item in g["items"]}


async def rows(web: Web) -> dict[str, Any]:
    async with web.res.sessionmaker() as s:
        result = await s.execute(select(AppSetting.key, AppSetting.value, AppSetting.updated_by))
        return {k: (v, by) for k, v, by in result.all() if k in app_settings.SPEC_BY_KEY}


@pytest.fixture
async def web(integration_settings: Settings) -> AsyncIterator[Web]:
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
        async with app.router.lifespan_context(app):
            res: Resources = app.state.res
            async with res.sessionmaker.begin() as s:
                await s.execute(delete(AppSetting))
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://ui.test",
                headers=AUTH | ORIGIN,
            ) as client:
                yield Web(client, settings, res)
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


async def test_get_lists_groups_without_secrets(web: Web) -> None:
    r = await web.client.get("/api/settings")
    assert r.status_code == 200
    data = r.json()
    assert data["version"] == 0
    assert [g["group"] for g in data["groups"]] == [g.key for g in app_settings.GROUPS]
    items = by_key(data)
    assert set(items) == set(app_settings.SPEC_BY_KEY)
    assert not set(items) & app_settings.ENV_ONLY
    poll = items["poll_interval_seconds"]
    assert poll["value"] == web.settings.poll_interval_seconds
    assert poll["source"] in {"default", "env"}
    assert (poll["kind"], poll["unit"], poll["min"], poll["live"]) == ("float", "giây", 10, True)
    assert items["llm_api_key"]["value"] is None
    assert items["llm_api_key"]["secret"]
    assert web.settings.database_url not in r.text
    assert "mat-khau" not in r.text


async def test_put_saves_bumps_version_and_publishes(web: Web) -> None:
    redis = web.res.redis
    pubsub = redis.pubsub()
    await pubsub.subscribe(app_settings.channel(web.prefix))
    try:
        r = await web.client.put(
            "/api/settings",
            json={
                "values": {
                    "poll_interval_seconds": 120,
                    "rebuild_on_error": False,
                    "llm_model": "qwen-test",
                    "rate_limits": {"WWW.TopCV.vn": 0.5},
                    "boards": ["topcv", "itviec"],
                }
            },
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["version"] == 1
        assert data["restart_required"] == ["llm_model", "rate_limits"]
        items = by_key(data)
        assert (
            items["poll_interval_seconds"]["value"],
            items["poll_interval_seconds"]["source"],
        ) == (
            120,
            "ui",
        )
        assert items["rebuild_on_error"]["value"] is False
        assert items["rate_limits"]["value"] == {"www.topcv.vn": 0.5}
        assert items["boards"]["value"] == ["topcv", "itviec"]
        message = None
        for _ in range(20):
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
            if message:
                break
        assert message is not None
        assert message["data"] in (b"1", "1")
    finally:
        await pubsub.unsubscribe()
        await pubsub.aclose()  # pyright: ignore[reportAttributeAccessIssue]

    stored = await rows(web)
    assert stored["poll_interval_seconds"] == (120.0, "admin")
    assert stored["rate_limits"] == ({"www.topcv.vn": 0.5}, "admin")
    assert await app_settings.settings_version(redis, web.prefix) == 1
    effective = await app_settings.load_effective(web.res.sessionmaker, web.settings)
    assert (effective.poll_interval_seconds, effective.llm_model) == (120, "qwen-test")

    # Lưu lại đúng giá trị cũ của bí mật (ô ẩn) không phải thay đổi: version giữ nguyên.
    r = await web.client.put("/api/settings", json={"values": {"serp_api_key": app_settings.MASK}})
    assert r.status_code == 200
    assert r.json()["version"] == 1


async def test_put_rejects_invalid_values_without_saving(web: Web) -> None:
    r = await web.client.put(
        "/api/settings",
        json={"values": {"poll_interval_seconds": 5, "rebuild_on_error": False, "boards": ["x"]}},
    )
    assert r.status_code == 422
    errors = r.json()["detail"]["errors"]
    assert set(errors) == {"poll_interval_seconds", "boards"}
    assert "10" in errors["poll_interval_seconds"]

    r = await web.client.put("/api/settings", json={"values": {"poll_deadline_seconds": 500}})
    assert r.status_code == 422
    assert set(r.json()["detail"]["errors"]) == {"poll_deadline_seconds"}

    r = await web.client.put(
        "/api/settings", json={"values": {"database_url": "postgresql://x", "khong_co": 1}}
    )
    assert r.status_code == 422
    assert set(r.json()["detail"]["errors"]) == {"database_url", "khong_co"}

    r = await web.client.put("/api/settings", json={"poll_interval_seconds": 120})
    assert r.status_code == 422  # thiếu `values`

    assert await rows(web) == {}
    assert await app_settings.settings_version(web.res.redis, web.prefix) == 0


async def test_secrets_nullable_and_reset(web: Web) -> None:
    r = await web.client.put(
        "/api/settings",
        json={"values": {"serp_api_key": "sk-bi-mat", "llm_requests_per_minute": ""}},
    )
    assert r.status_code == 200, r.text
    assert "sk-bi-mat" not in r.text
    items = by_key(r.json())
    assert (items["serp_api_key"]["is_set"], items["serp_api_key"]["source"]) == (True, "ui")
    assert items["serp_api_key"]["value"] is None
    assert items["llm_requests_per_minute"]["value"] is None
    assert items["llm_requests_per_minute"]["source"] == "ui"
    stored = await rows(web)
    assert stored["serp_api_key"][0] == "sk-bi-mat"
    assert stored["llm_requests_per_minute"][0] is None  # JSON null, khác "không có dòng"
    effective = await app_settings.load_effective(web.res.sessionmaker, web.settings)
    assert effective.serp_api_key == SecretStr("sk-bi-mat")

    r = await web.client.delete("/api/settings/serp_api_key")
    assert r.status_code == 200
    items = by_key(r.json())
    assert (items["serp_api_key"]["is_set"], items["serp_api_key"]["source"]) == (False, "env")
    assert "serp_api_key" not in await rows(web)
    assert r.json()["version"] == 2

    assert (await web.client.delete("/api/settings/database_url")).status_code == 404
    assert (await web.client.delete("/api/settings/khong_co")).status_code == 404


async def test_cross_site_write_is_blocked(web: Web) -> None:
    r = await web.client.put(
        "/api/settings",
        json={"values": {"poll_interval_seconds": 120}},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403
    assert await rows(web) == {}


async def test_workers_list_and_drain(web: Web) -> None:
    redis = web.res.redis
    keys = registry.Keys(web.prefix)
    alive = registry.WorkerStatus(
        worker_id="w-alive",
        hostname="may-1",
        version="0.1.0",
        pool="default",
        http_slots=8,
        render_slots=2,
        local_proxies=3,
        stats=registry.WorkerStats(http_inflight=3, render_inflight=1, http_done=40, http_failed=2),
    )
    gone = registry.WorkerStatus(
        worker_id="w-gone",
        pool="residential",
        http_slots=4,
        stats=registry.WorkerStats(http_done=5, render_failed=1),
    )
    await registry.save_worker(redis, keys, alive, ttl=30)
    await registry.save_worker(redis, keys, gone, ttl=30)
    # Hạn báo sống đã qua: coi như đã rời (vẫn còn thông tin trong TTL của `worker:<id>`).
    await redis.zadd(keys.workers, {"w-gone": int(time.time() * 1000) - 1000})

    data = (await web.client.get("/api/workers")).json()
    ids = [w["worker_id"] for w in data["workers"]]
    assert ids == ["w-alive", "w-gone"]
    assert [w["alive"] for w in data["workers"]] == [True, False]
    summary = data["summary"]
    assert (summary["alive"], summary["gone"], summary["draining"]) == (1, 1, 0)
    assert (summary["http_slots"], summary["http_inflight"]) == (8, 3)
    assert (summary["render_slots"], summary["render_inflight"]) == (2, 1)
    assert (summary["done"], summary["failed"]) == (45, 3)
    assert summary["pools"] == {
        "default": {
            "workers": 1,
            "http_slots": 8,
            "render_slots": 2,
            "http_inflight": 3,
            "render_inflight": 1,
        }
    }
    assert data["now_ms"] > 0

    r = await web.client.post("/api/workers/w-alive/drain")
    assert r.status_code == 200
    assert r.json() == {"worker_id": "w-alive", "draining": True}
    assert await registry.is_draining(redis, keys, "w-alive")
    data = (await web.client.get("/api/workers")).json()
    assert data["workers"][0]["draining"] is True
    assert data["summary"]["draining"] == 1

    r = await web.client.delete("/api/workers/w-alive/drain")
    assert r.status_code == 200
    assert not await registry.is_draining(redis, keys, "w-alive")

    assert (await web.client.post("/api/workers/khong-co/drain")).status_code == 404
    assert (await web.client.delete("/api/workers/khong-co/drain")).status_code == 404
    evil = await web.client.post(
        "/api/workers/w-alive/drain", headers={"Origin": "https://evil.example"}
    )
    assert evil.status_code == 403


async def test_empty_workers(web: Web) -> None:
    data = (await web.client.get("/api/workers")).json()
    assert data["workers"] == []
    assert data["summary"]["alive"] == 0
    assert data["summary"]["pools"] == {}
