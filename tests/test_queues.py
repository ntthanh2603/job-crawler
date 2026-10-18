from __future__ import annotations

import dataclasses
import json

from jobcrawler.db.ids import uuid7
from jobcrawler.pipeline.board_index import IndexStats
from jobcrawler.queues import queue_from_url


def test_job_results_with_uuid_are_json() -> None:
    # Kết quả `index_board` (IndexStats.run_id là UUID) đi qua Redis dạng JSON.
    run_id = uuid7()
    queue = queue_from_url("redis://localhost:6379/0", "jobcrawler-test")
    stats = IndexStats(source="itviec", run_id=run_id)
    payload = json.loads(queue._dump({"result": dataclasses.asdict(stats)}))
    assert payload["result"]["run_id"] == str(run_id)
    assert payload["result"]["status"] == "running"
