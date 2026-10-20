"""Payload các service dựng sẵn thành dict (trang tổng quan, độ trễ phát hiện, Cài đặt) khớp mô hình
trả về của API: thiếu trường thì endpoint lỗi 500, còn trường thừa bị bỏ lặng lẽ — test này bắt cả
hai (`extra="forbid"`)."""

from __future__ import annotations

import dataclasses
from uuid import uuid4

import pytest

from jobcrawler.bootstrap.app import create_app
from jobcrawler.common.config import Settings
from jobcrawler.modules.crawl.actions import crawl_status
from jobcrawler.modules.crawl.schemas import CrawlStatusOut
from jobcrawler.modules.overview import overview, stats
from jobcrawler.modules.overview.schemas import OverviewOut, StatsOut
from jobcrawler.modules.settings import app_settings
from jobcrawler.modules.settings.schemas import SettingsPageOut
from jobcrawler.services.redis.queues import drop_queue
from jobcrawler.services.resources import Resources

from .test_web_db import seed

pytestmark = pytest.mark.integration


async def test_service_payloads_have_no_untyped_keys(integration_settings: Settings) -> None:
    tag = uuid4().hex[:6]
    settings = integration_settings.model_copy(
        update={
            "redis_prefix": f"jc-contract-{tag}",
            "build_queue_name": f"jobcrawler-test-build-{tag}",
            "poll_queue_name": f"jobcrawler-test-poll-{tag}",
            "board_queue_name": f"jobcrawler-test-boards-{tag}",
            "llm_base_url": None,
            "serp_api_key": None,
        }
    )
    app = create_app(settings)
    try:
        async with app.router.lifespan_context(app):
            await seed(app.state, tag)
            res: Resources = app.state.res
            st = await stats.collect_stats(res, hours=None, storage=True)
            StatsOut.model_validate(dataclasses.asdict(st), extra="forbid")
            view = await overview.build_overview(res, hours=1, storage=True)
            assert view["recent_runs"]
            OverviewOut.model_validate(view, extra="forbid")
            # Lần hai đọc JSON cache trong Redis.
            cached = await overview.build_overview(res, hours=1, storage=True)
            OverviewOut.model_validate(cached, extra="forbid")
            CrawlStatusOut.model_validate(await crawl_status(res), extra="forbid")
            groups = app_settings.describe(settings, {}, settings)
            SettingsPageOut.model_validate(
                {
                    "version": 0,
                    "groups": groups,
                    "env_keys": sorted(app_settings.env_overrides(settings, {})),
                    "auto_restart": False,
                    "restart_pending": False,
                },
                extra="forbid",
            )
    finally:
        for name in settings.queue_names:
            await drop_queue(settings.redis_url, name)
