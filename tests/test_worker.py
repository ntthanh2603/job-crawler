from __future__ import annotations

from jobcrawler.config import Settings
from jobcrawler.entrypoints import worker


def test_worker_registers_index_cron(clean_env: None) -> None:
    settings = Settings(board_index_cron="0 */4 * * *", board_index_budget_seconds=600)
    w = worker.build_worker(settings)
    assert {"process_company", "index_board"} <= set(w.functions)
    [cron] = w.cron_jobs
    assert cron.cron == "0 */4 * * *"
    assert cron.function is worker.index_boards_cron
    assert worker.board_job_timeout(settings) == 1500
