"""Mã Python sinh từ crawl.proto phải khớp file .proto đã commit."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "gen.py"


def test_generated_code_is_current() -> None:
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr


def test_messages_round_trip() -> None:
    from jcproto.v1 import crawl_pb2

    task = crawl_pb2.FetchTask(
        task_id="t1",
        kind=crawl_pb2.TASK_KIND_RENDER,
        url="https://example.com/jobs",
        headers={"accept": "text/html"},
        capture_json=True,
    )
    again = crawl_pb2.FetchTask.FromString(task.SerializeToString())
    assert again == task
    assert again.kind == crawl_pb2.TASK_KIND_RENDER
