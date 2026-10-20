from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

from jobcrawler.bootstrap import runner
from jobcrawler.common.config import Settings
from jobcrawler.common.schemas import CompanyResult, RunStatus, RunTrigger
from jobcrawler.modules.crawl import companies, crawl
from jobcrawler.services.redis.streamqueue import StreamWorker


def test_build_role_runs_builds_maintenance_and_overview(clean_env: None) -> None:
    settings = Settings(
        runner_build_concurrency=7, maintenance_cron="5 * * * *", overview_cron="*/3 * * * *"
    )
    w = runner.build_worker(settings, "build")
    assert isinstance(w, StreamWorker)
    assert set(w.functions) == {"build_company", "maintenance_cron", "overview_cron"}
    assert (w.queue.name, w.concurrency) == ("jobcrawler-build", 7)
    maintenance, overview = w.cron_jobs
    assert (maintenance.cron, maintenance.function) == ("5 * * * *", runner.maintenance_cron)
    assert (overview.cron, overview.function) == ("*/3 * * * *", runner.overview_cron)
    assert maintenance.unique  # nhiều replica vẫn chỉ một lượt mỗi mốc
    assert w._signals == ()  # runner tự bắt tín hiệu cho mọi vai


def test_poll_role_has_own_queue_and_no_cron(clean_env: None) -> None:
    w = runner.build_worker(Settings(runner_poll_concurrency=64), "poll")
    assert set(w.functions) == {"poll_company"}
    assert (w.queue.name, w.concurrency, w.cron_jobs) == ("jobcrawler-poll", 64, [])


def test_board_role_runs_full_index_at_night_and_tail_by_day(clean_env: None) -> None:
    settings = Settings(
        board_index_cron="30 0 * * *",
        board_tail_cron="*/2 6-23 * * *",
        board_index_budget_seconds=600,
        board_tail_budget_seconds=90,
        runner_board_concurrency=3,
    )
    w = runner.build_worker(settings, "boards")
    assert set(w.functions) == {
        "index_board",
        "tail_board",
        "index_boards_cron",
        "tail_boards_cron",
    }
    assert (w.queue.name, w.concurrency) == ("jobcrawler-boards", 3)
    full, tail = w.cron_jobs
    assert (full.cron, full.function) == ("30 0 * * *", runner.index_boards_cron)
    assert (tail.cron, tail.function) == ("*/2 6-23 * * *", runner.tail_boards_cron)
    assert runner.board_job_timeout(settings) == 1500
    assert runner.board_tail_timeout(settings) == 210


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


class FakeSession:
    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get(self, _model: Any, _id: Any) -> Any:
        return SimpleNamespace(input_name="ACME", tax_code=None, input_website="acme.vn", note=None)


@pytest.mark.parametrize(
    ("status", "continuation", "queued"),
    [
        (RunStatus.PARTIAL, 0, True),
        (RunStatus.PARTIAL, 1, True),
        (RunStatus.PARTIAL, 2, False),  # đã chạy tiếp đủ COMPANY_MAX_CONTINUATIONS lần
        (RunStatus.OK, 0, False),
    ],
)
async def test_partial_build_enqueues_a_continuation(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
    status: RunStatus,
    continuation: int,
    queued: bool,
) -> None:
    seen: dict[str, Any] = {}

    async def fake_process(res: Any, company_in: Any, **kwargs: Any) -> CompanyResult:
        seen.update(kwargs)
        return _result(status)

    @asynccontextmanager
    async def no_lock(*_args: Any, **_kwargs: Any) -> AsyncIterator[bool]:
        yield True

    monkeypatch.setattr(companies, "process_company", fake_process)
    monkeypatch.setattr(crawl, "company_lock", no_lock)
    queue = RecordingQueue()
    settings = Settings(company_max_continuations=2, company_continuation_delay_seconds=60)
    res = SimpleNamespace(
        settings=settings,
        queues=SimpleNamespace(build=queue),
        sessionmaker=FakeSession,
    )
    ctx: Any = {"settings": settings, "resources": res}
    out = await runner.build_company(
        ctx, company_id=str(UUID(int=2)), trigger="nightly", continuation=continuation
    )
    assert out["status"] == status.value
    # Build đêm lượt đầu tìm lại nguồn; lượt chạy tiếp dùng cache tìm nguồn.
    assert seen["refresh_discovery"] is (continuation == 0)
    assert seen["trigger"] is RunTrigger.NIGHTLY
    if not queued:
        assert queue.jobs == []
        return
    [(function, kwargs)] = queue.jobs
    assert function == "build_company"
    assert kwargs["key"] == f"build:{UUID(int=2)}:cont{continuation + 1}"
    assert kwargs["continuation"] == continuation + 1
    assert kwargs["company_id"] == str(UUID(int=2))
    assert kwargs["trigger"] == RunTrigger.CONTINUATION.value
    assert kwargs["scheduled"] >= datetime.now(UTC).timestamp() + 50


async def test_build_is_skipped_while_another_run_holds_the_company(
    monkeypatch: pytest.MonkeyPatch, clean_env: None
) -> None:
    called: list[Any] = []

    async def fake_process(*args: Any, **kwargs: Any) -> CompanyResult:
        called.append(args)
        raise AssertionError("không được chạy khi không giữ khoá")

    @asynccontextmanager
    async def busy_lock(*_args: Any, **kwargs: Any) -> AsyncIterator[bool]:
        assert kwargs["wait"] > Settings().poll_timeout_seconds  # chờ lượt poll đang chạy xong
        yield False

    monkeypatch.setattr(companies, "process_company", fake_process)
    monkeypatch.setattr(crawl, "company_lock", busy_lock)
    settings = Settings()
    res = SimpleNamespace(settings=settings, sessionmaker=FakeSession)
    ctx: Any = {"settings": settings, "resources": res}
    out = await runner.build_company(ctx, company_id=str(UUID(int=2)))
    assert out["status"] == "skipped"
    assert "đang có lượt" in out["error"]
    assert called == []


@pytest.mark.parametrize(
    ("watchdog", "reclaim", "limit"),
    [(120, 60, 30), (20, 60, 20), (0, 60, 0), (120, 300, 120)],
)
def test_runner_watchdog_fires_before_reclaim(
    watchdog: float, reclaim: float, limit: float, clean_env: None
) -> None:
    # Runner đứng phải tự thoát trước khi job của nó bị runner khác thu hồi và chạy lại.
    settings = Settings(watchdog_seconds=watchdog, queue_reclaim_seconds=reclaim)
    assert runner.stall_limit(settings) == limit


async def test_bad_company_id_is_reported_not_raised(clean_env: None) -> None:
    ctx: Any = {"settings": Settings(), "resources": SimpleNamespace()}
    assert "error" in await runner.build_company(ctx, company_id="khong-phai-uuid")
    assert "error" in await runner.poll_company(ctx, company_id="khong-phai-uuid")


def test_job_ready_at_uses_schedule_when_later() -> None:
    job = SimpleNamespace(queued=1_760_000_000_000, scheduled=0)
    assert runner.job_ready_at(job) == datetime.fromtimestamp(1_760_000_000, UTC)
    # Lượt chạy tiếp hẹn sau 60 s: thời gian chờ tính từ giờ hẹn.
    job.scheduled = 1_760_000_060
    assert runner.job_ready_at(job) == datetime.fromtimestamp(1_760_000_060, UTC)
    assert runner.job_ready_at(None) is None
