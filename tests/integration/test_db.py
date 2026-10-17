from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from sqlalchemy import func, select

from jobcrawler.config import Settings
from jobcrawler.db import migrate
from jobcrawler.db.models import Company, CompanyRun, Log, SourceFetch
from jobcrawler.fakes import FakeFetcher
from jobcrawler.logging import configure_logging
from jobcrawler.resources import open_resources
from jobcrawler.schemas import CompanyIn, RunStatus
from jobcrawler.services.companies import process_company

FIXTURES = Path(__file__).parent.parent / "fixtures"

pytestmark = pytest.mark.integration


def test_models_match_migrations(test_database_url: str) -> None:
    migrate.check(database_url=test_database_url)


async def test_process_company_writes_run_and_logs(integration_settings: Settings) -> None:
    configure_logging(integration_settings)
    async with open_resources(integration_settings) as res:
        first = await process_company(res, CompanyIn(name="Công ty TNHH Integration Test"))
        second = await process_company(res, CompanyIn(name="Integration Test JSC"))

    assert first.status is RunStatus.OK
    assert second.company_id == first.company_id  # cùng alias_norm → không tạo trùng

    # open_resources đã đóng: sink log đã flush xong.
    async with (
        open_resources(integration_settings, db_logs=False) as res,
        res.sessionmaker() as session,
    ):
        run = await session.get(CompanyRun, first.run_id)
        companies = await session.scalar(
            select(func.count()).select_from(Company).where(Company.id == first.company_id)
        )
        events = (
            await session.scalars(
                select(Log.event).where(Log.run_id == first.run_id).order_by(Log.id)
            )
        ).all()
        company_ids = set(
            await session.scalars(select(Log.company_id).where(Log.run_id == first.run_id))
        )

    assert run is not None
    assert run.status == "ok"
    assert run.finished_at is not None
    assert run.duration_ms is not None
    assert companies == 1
    assert events == [
        "company.start",
        "kyc.serp_disabled",
        "kyc.done",
        "company.no_website",
        "company.done",
    ]
    assert company_ids == {first.company_id}


async def test_process_company_with_website_records_source_fetches(
    integration_settings: Settings,
) -> None:
    """Có website (ở đây là URL bảng Greenhouse) → tìm nguồn + thu thập, ghi `source_fetches`."""
    settings = integration_settings.model_copy(update={"redis_prefix": "jc-test"})
    configure_logging(settings)
    fetcher = FakeFetcher()
    fetcher.add_file(
        "https://boards-api.greenhouse.io/v1/boards/gitlab/jobs?content=true",
        FIXTURES / "greenhouse/gitlab_jobs.json",
    )
    async with open_resources(settings, db_logs=False) as res:
        res = dataclasses.replace(res, http=fetcher)  # pyright: ignore[reportArgumentType]
        result = await process_company(
            res,
            CompanyIn(name="GitLab Integration", website="https://job-boards.greenhouse.io/gitlab"),
        )
        async with res.sessionmaker() as session:
            run = await session.get(CompanyRun, result.run_id)
            fetches = (
                await session.scalars(
                    select(SourceFetch).where(SourceFetch.run_id == result.run_id)
                )
            ).all()

    assert result.status is RunStatus.OK
    assert result.jobs_found == 3
    assert run is not None
    assert run.jobs_found == 3
    [fetch] = fetches
    assert fetch.company_id == result.company_id
    assert fetch.source == "greenhouse:gitlab"
    assert fetch.url == "https://boards-api.greenhouse.io/v1/boards/gitlab/jobs?content=true"
    assert (fetch.http_status, fetch.items, fetch.outcome) == (200, 3, "ok")
