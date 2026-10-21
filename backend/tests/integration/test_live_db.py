"""Cập nhật trực tiếp (`GET /api/events`) trên Postgres + Redis thật: mỗi chủ đề trả đúng dữ liệu
của endpoint JSON tương ứng và đẩy khi Redis / DB đổi."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from redis.asyncio import Redis

from jobcrawler.bootstrap.app import create_app
from jobcrawler.common.config import Settings
from jobcrawler.modules.auth.auth_service import COOKIE
from jobcrawler.modules.gateway import registry_service
from jobcrawler.modules.live import live_service
from jobcrawler.services.redis.queues import drop_queue

from ..test_live import FAST, SseStream
from .test_web_db import seed

pytestmark = pytest.mark.integration


@dataclass
class Web:
    app: FastAPI
    settings: Settings
    tag: str
    token: str

    def stream(self, query: str) -> SseStream:
        return SseStream(self.app, f"/api/events?{query}", {"cookie": f"{COOKIE}={self.token}"})

    async def get(self, path: str) -> Any:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="http://ui.test",
            headers={"Authorization": f"Bearer {self.token}"},
        ) as client:
            r = await client.get(path)
            assert r.status_code == 200
            return r.json()


@pytest.fixture
async def web(integration_settings: Settings) -> AsyncIterator[Web]:
    tag = uuid4().hex[:6]
    settings = integration_settings.model_copy(
        update={
            "redis_prefix": f"jc-live-{tag}",
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
            # Hub của lifespan, chỉ đọc nhanh hơn cho test.
            app.state.live.close()
            loader = live_service.resource_loader(app.state.res, settings)
            app.state.live = live_service.LiveHub(loader, intervals=FAST, linger=0.05)
            yield Web(app, settings, tag, app.state.token_auth.issue().token)
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


async def test_topics_match_json_endpoints_and_push_changes(web: Web) -> None:
    res = web.app.state.res
    keys = registry_service.Keys(web.settings.redis_prefix)
    worker = registry_service.WorkerStatus(worker_id="w-live", hostname="may-1", http_slots=4)
    await registry_service.save_worker(res.redis, keys, worker, ttl=30)
    _, run_id, *_ = await seed(web.app.state, web.tag)

    query = f"topic=workers&topic=queue&topic=run&run_id={run_id}"
    async with web.stream(query) as s:
        assert s.status == 200
        got: dict[str, Any] = {}
        while len(got) < 3:
            event = (await s.next_data()).json
            got[event["topic"]] = event[event["topic"]]

        workers = await web.get("/api/workers")
        assert got["workers"] | {"now_ms": 0} == workers | {"now_ms": 0}
        assert [w["worker_id"] for w in got["workers"]["workers"]] == ["w-live"]
        assert got["queue"] == await web.get("/api/queue")
        assert got["run"] == await web.get(f"/api/runs/{run_id}")
        assert got["run"]["fetches_total"] == 2

        # Việc mới vào hàng đợi build: sự kiện queue mới, không phải tải lại.
        await res.queues.build.enqueue("build_company", key=f"live-{web.tag}", name="Web live")
        # Số đếm và danh sách job đọc bằng hai lệnh Redis: lần đọc chen giữa lúc xếp job có thể
        # thấy một nửa (lần đọc sau, ~2 s ở production, đã khớp) — chờ bản đủ cả hai.
        async with asyncio.timeout(10):
            while True:
                event = (await s.next_data()).json
                if event["topic"] != "queue":
                    continue
                build = event["queue"]["queues"][0]
                if build["queued"] and build["jobs"]:
                    break
        assert (build["queued"], [j["label"] for j in build["jobs"]]) == (1, ["Web live"])
