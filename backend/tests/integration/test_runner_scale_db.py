"""Runner theo năng lực worker trên Redis thật: số lượt build / poll song song đi theo tổng slot
HTTP của worker đang kết nối (`runner.live_concurrency`), như open-asm."""

from __future__ import annotations

from uuid import uuid4

import pytest

from jobcrawler.bootstrap.runner import live_concurrency
from jobcrawler.common import sysres
from jobcrawler.common.config import Settings
from jobcrawler.modules.gateway import registry_service
from jobcrawler.services.resources import open_resources

pytestmark = pytest.mark.integration


async def test_more_workers_raise_runner_concurrency(integration_settings: Settings) -> None:
    settings = integration_settings.model_copy(update={"redis_prefix": f"jctest:{uuid4().hex[:8]}"})
    async with open_resources(settings, db_logs=False) as res:
        keys = registry_service.Keys(settings.redis_prefix)
        assert await registry_service.live_http_slots(res.redis, keys) == 0
        alone = await live_concurrency(res, "build", processes=1)
        assert alone == min(25, sysres.auto_concurrency("build"))  # chưa có worker: theo máy

        for i in range(4):
            worker = registry_service.WorkerStatus(worker_id=f"w-scale-{i}", http_slots=64)
            await registry_service.save_worker(res.redis, keys, worker, ttl=30)
        assert await registry_service.live_http_slots(res.redis, keys) == 256
        assert await live_concurrency(res, "build", processes=1) == sysres.scaled_concurrency(
            "build", slots=256, limit=25, processes=1
        )
        assert await live_concurrency(res, "poll", processes=2) == sysres.scaled_concurrency(
            "poll", slots=256, limit=60, processes=2
        )
        assert await live_concurrency(res, "build", processes=1) > alone

        low = settings.model_copy(update={"runner_build_concurrency": 3})
        res.settings = low  # pyright: ignore[reportAttributeAccessIssue]
        assert await live_concurrency(res, "build", processes=1) == 3  # cài đặt là mức tối đa
