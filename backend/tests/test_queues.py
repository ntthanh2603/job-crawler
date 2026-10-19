from __future__ import annotations

import dataclasses
import json

from jobcrawler.config import Settings
from jobcrawler.db.ids import uuid7
from jobcrawler.pipeline.board_index import IndexStats
from jobcrawler.queues import Queues, make_queue
from jobcrawler.streamqueue import StreamJob, StreamQueue, dump_json


def test_job_results_with_uuid_are_json() -> None:
    # Kết quả `index_board` (IndexStats.run_id là UUID) đi qua Redis dạng JSON.
    run_id = uuid7()
    stats = IndexStats(source="itviec", run_id=run_id)
    payload = json.loads(dump_json({"result": dataclasses.asdict(stats)}))
    assert payload["result"]["run_id"] == str(run_id)
    assert payload["result"]["status"] == "running"


def test_make_queue_uses_streams(clean_env: None) -> None:
    default = make_queue(Settings(queue_reclaim_seconds=30), "jobcrawler-build")
    assert isinstance(default, StreamQueue)
    assert (default.name, default.reclaim_seconds) == ("jobcrawler-build", 30)
    assert default.stream == "sq:jobcrawler-build:stream"


def test_three_queues_from_settings(clean_env: None) -> None:
    queues = Queues.from_settings(Settings(poll_queue_name="p"))
    assert [q.name for q in queues.all()] == ["jobcrawler-build", "p", "jobcrawler-boards"]


def test_stream_job_round_trips_through_redis_hash() -> None:
    run_id = uuid7()
    h = {
        "key": "company:acme",
        "function": "process_company",
        "kwargs": dump_json({"company_id": run_id, "continuation": 1}),
        "status": "active",
        "attempts": "1",
        "retries": "2",
        "timeout": "180",
        "queued": "1760000000000",
        "scheduled": "1760000060",
        "started": "1760000061000",
        "result": dump_json({"status": "ok"}),
        "worker": "w1",
        "entry": "1760000060000-0",
    }
    job = StreamJob.from_hash(h)
    assert job.kwargs == {"company_id": str(run_id), "continuation": 1}
    assert (job.attempts, job.retries, job.timeout, job.scheduled) == (1, 2, 180, 1760000060)
    assert job.result == {"status": "ok"}
    out = job.to_dict()
    # Các trường trang Hàng đợi đọc.
    assert out["queued"] == 1760000000000
    assert out["started"] == 1760000061000
    assert out["worker_id"] == "w1"
    assert out["error"] is None
