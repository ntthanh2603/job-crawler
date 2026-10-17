from __future__ import annotations

from jobcrawler.config import Settings
from jobcrawler.entrypoints import worker


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
