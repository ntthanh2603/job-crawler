from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import pytest

from jobcrawler.config import Settings
from jobcrawler.entrypoints import worker
from jobcrawler.schemas import CompanyResult, RunStatus
from jobcrawler.services import companies


def test_company_worker_runs_companies_maintenance_and_overview(clean_env: None) -> None:
    settings = Settings(
        worker_concurrency=7, maintenance_cron="5 * * * *", overview_cron="*/3 * * * *"
    )
    w = worker.build_worker(settings)
    assert set(w.functions) == {"process_company", "maintenance_cron", "overview_cron"}
    assert w.queue.name == "jobcrawler"
    assert w.concurrency == 7
    maintenance, overview = w.cron_jobs
    assert (maintenance.cron, maintenance.function) == ("5 * * * *", worker.maintenance_cron)
    assert (overview.cron, overview.function) == ("*/3 * * * *", worker.overview_cron)
    assert maintenance.unique  # nhiều replica vẫn chỉ một lượt mỗi mốc
    assert overview.unique


def test_board_worker_has_own_queue_and_index_cron(clean_env: None) -> None:
    settings = Settings(
        board_index_cron="0 */4 * * *", board_index_budget_seconds=600, board_worker_concurrency=3
    )
    w = worker.build_worker(settings, "boards")
    assert set(w.functions) == {"index_board", "index_boards_cron"}
    assert w.queue.name == "jobcrawler-boards" != worker.queue_name(settings)
    assert w.concurrency == 3
    [cron] = w.cron_jobs
    assert cron.cron == "0 */4 * * *"
    assert cron.function is worker.index_boards_cron
    assert worker.board_job_timeout(settings) == 1500


class RecordingQueue:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, dict[str, Any]]] = []

    async def enqueue(self, function: str, **kwargs: Any) -> object:
        self.jobs.append((function, kwargs))
        return object()


def _result(status: RunStatus) -> CompanyResult:
    now = datetime.now(UTC)
    return CompanyResult(
        run_id=UUID(int=1),
        company_id=UUID(int=2),
        company_name="ACME",
        status=status,
        started_at=now,
        finished_at=now,
        duration_ms=1,
        incomplete_sources=["careers:acme.vn"] if status is RunStatus.PARTIAL else [],
    )


@pytest.mark.parametrize(
    ("status", "continuation", "queued"),
    [
        (RunStatus.PARTIAL, 0, True),
        (RunStatus.PARTIAL, 1, True),
        (RunStatus.PARTIAL, 2, False),  # đã chạy tiếp đủ COMPANY_MAX_CONTINUATIONS lần
        (RunStatus.OK, 0, False),
    ],
)
async def test_partial_run_enqueues_a_continuation(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
    status: RunStatus,
    continuation: int,
    queued: bool,
) -> None:
    async def fake_process(res: Any, company_in: Any, *, company_id: Any) -> CompanyResult:
        return _result(status)

    monkeypatch.setattr(companies, "process_company", fake_process)
    queue = RecordingQueue()
    settings = Settings(company_max_continuations=2, company_continuation_delay_seconds=60)
    res = type("Res", (), {"settings": settings, "queue": queue})()
    ctx = cast(worker.WorkerContext, {"settings": settings, "resources": res})
    out = await worker.process_company(
        ctx, name="ACME", website="acme.vn", company_id=str(UUID(int=2)), continuation=continuation
    )
    assert out["status"] == status.value
    if not queued:
        assert queue.jobs == []
        return
    [(function, kwargs)] = queue.jobs
    assert function == "process_company"
    assert kwargs["key"] == f"company:acme:cont{continuation + 1}"
    assert kwargs["continuation"] == continuation + 1
    assert kwargs["company_id"] == str(UUID(int=2))
    assert kwargs["website"] == "acme.vn"
    assert kwargs["scheduled"] >= datetime.now(UTC).timestamp() + 50
