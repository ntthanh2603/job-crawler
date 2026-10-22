"""Cảnh báo trên Redis thật: nguồn số đo của `alert_service` (độ trễ phát hiện, worker, LLM)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import BaseModel

from jobcrawler.common.config import Settings
from jobcrawler.modules.alerts import alert_service
from jobcrawler.modules.crawl import sla_service
from jobcrawler.modules.gateway import registry_service
from jobcrawler.services.llm import LLMResult
from jobcrawler.services.llm_metrics import LLMMetrics
from jobcrawler.services.resources import open_resources

pytestmark = pytest.mark.integration

# 10:00 giờ Việt Nam: trong khung poll mặc định.
NOW = datetime(2026, 10, 22, 3, 0, tzinfo=UTC)


class Answer(BaseModel):
    ok: bool = True


async def test_probes_read_live_metrics(integration_settings: Settings) -> None:
    settings = integration_settings.model_copy(
        update={
            "redis_prefix": f"jctest:{uuid4().hex[:8]}",
            "llm_base_url": "http://llm.test/v1",
            "llm_model": "qwen",
            "alert_min_workers": 2,
        }
    )
    async with open_resources(settings, db_logs=False) as res:
        prefix = settings.redis_prefix
        # Độ trễ phát hiện: 30 lượt poll, cửa sổ 200 s > ngưỡng 180 s.
        for _ in range(30):
            await sla_service.record_poll(
                res.redis, prefix, status="ok", duration_s=3, window_s=200, now=NOW.timestamp()
            )
        [late] = await alert_service.detection(res, NOW)
        assert late.firing is True
        assert "210 s" in late.message  # cận trên của ô histogram chứa 200 s
        night = settings.model_copy(update={"poll_window": "00:00-01:00"})
        quiet = dataclasses.replace(res, settings=night)
        [outside] = await alert_service.detection(quiet, NOW)
        assert outside.firing is None

        # Worker: một worker sống (< 2), 4/5 slot HTTP đang dùng.
        keys = registry_service.Keys(prefix)
        worker = registry_service.WorkerStatus(
            worker_id="w-alert",
            http_slots=5,
            stats=registry_service.WorkerStats(http_inflight=4),
        )
        await registry_service.save_worker(res.redis, keys, worker, ttl=30)
        found = {f.kind: f for f in await alert_service.workers(res, NOW)}
        assert found["workers"].firing is True
        assert found["http"].firing is True
        assert "4/5" in found["http"].message

        # LLM: 25 lời gọi, 10 lỗi (40% > 20%).
        metrics = LLMMetrics(res.redis, prefix=prefix)
        for i in range(25):
            failed = i < 10
            result = LLMResult[Answer](
                value=None if failed else Answer(),
                prompt="job_verify",
                version="1",
                model="qwen",
                error="timeout" if failed else None,
            )
            await metrics.record(result, now=NOW.timestamp())
        [llm] = await alert_service.llm(res, NOW)
        assert llm.firing is True
        assert "10/25" in llm.message
