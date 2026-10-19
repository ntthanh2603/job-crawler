from __future__ import annotations

import asyncio
import json
import threading
from uuid import UUID, uuid4

import pytest
import structlog

from jobcrawler import logging as jlog
from jobcrawler.config import Settings


class FakeWriter:
    def __init__(self, *, fail: bool = False) -> None:
        self.batches: list[list[jlog.LogRow]] = []
        self.fail = fail

    async def __call__(self, rows: list[jlog.LogRow]) -> None:
        if self.fail:
            raise RuntimeError("db down")
        self.batches.append(rows)

    @property
    def rows(self) -> list[jlog.LogRow]:
        return [r for b in self.batches for r in b]


def test_event_to_row_splits_columns_and_context() -> None:
    run_id, company = uuid4(), uuid4()
    ts, level, event, rid, company_id, context, row_id = jlog.event_to_row(
        {
            "event": "company.start",
            "level": "info",
            "timestamp": "2026-01-01T00:00:00Z",
            "run_id": str(run_id),
            "company_id": company,
            "company": "Công ty ABC",
            "_record": object(),
        }
    )
    assert ts.tzinfo is not None
    assert (level, event, rid, company_id) == ("info", "company.start", run_id, company)
    assert json.loads(context) == {"company": "Công ty ABC"}
    assert row_id.version == 7


def test_event_to_row_tolerates_bad_ids_and_formats_exceptions() -> None:
    try:
        raise ValueError("boom")
    except ValueError as exc:
        error = exc
    row = jlog.event_to_row(
        {
            "event": "x",
            "level": "error",
            "run_id": "not-a-uuid",
            "company_id": True,
            "exc_info": error,
        }
    )
    assert row[3] is None
    assert row[4] is None
    context = json.loads(row[5])
    assert "ValueError: boom" in context["exception"]


async def test_sink_flushes_on_close_and_filters_level() -> None:
    writer = FakeWriter()
    sink = jlog.PgLogSink(writer, min_level="INFO", interval=60)
    sink.start()
    sink.emit({"event": "debug.noise", "level": "debug"})
    for i in range(3):
        sink.emit({"event": f"e{i}", "level": "info"})
    sink.emit({"event": "w", "level": "warning"})
    assert writer.rows == []  # chưa tới chu kỳ ghi: emit không chờ DB
    await sink.close()
    assert [r[2] for r in writer.rows] == ["e0", "e1", "e2", "w"]
    assert sink.written == 4


async def test_sink_writes_in_background_batches() -> None:
    writer = FakeWriter()
    sink = jlog.PgLogSink(writer, interval=0.01, batch_size=2)
    sink.start()
    for i in range(5):
        sink.emit({"event": f"e{i}", "level": "info"})
    for _ in range(100):
        if len(writer.rows) == 5:
            break
        await asyncio.sleep(0.01)
    assert [len(b) for b in writer.batches] == [2, 2, 1]
    await sink.close()


async def test_sink_drops_when_full_and_survives_writer_errors() -> None:
    writer = FakeWriter(fail=True)
    sink = jlog.PgLogSink(writer, interval=60, max_queue=2)
    sink.start()
    for i in range(5):
        sink.emit({"event": f"e{i}", "level": "info"})
    assert sink.dropped == 3
    await sink.close()  # writer lỗi: không ném ra ngoài
    assert sink.dropped == 5
    sink.emit({"event": "after-close", "level": "info"})  # bỏ qua sau khi đóng


async def test_sink_accepts_events_from_other_threads() -> None:
    writer = FakeWriter()
    sink = jlog.PgLogSink(writer, interval=60)
    sink.start()
    thread = threading.Thread(target=sink.emit, args=({"event": "thread", "level": "info"},))
    thread.start()
    thread.join()
    await asyncio.sleep(0)  # chạy callback call_soon_threadsafe
    await sink.close()
    assert [r[2] for r in writer.rows] == ["thread"]


@pytest.mark.usefixtures("clean_env")
async def test_structlog_events_reach_sink_with_contextvars() -> None:
    jlog.configure_logging(Settings(_env_file=None))  # pyright: ignore[reportCallIssue]
    writer = FakeWriter()
    sink = jlog.PgLogSink(writer, interval=60)
    sink.start()
    jlog.attach_sink(sink)
    run_id, company_id = uuid4(), uuid4()
    try:
        log = jlog.get_logger("test")
        with structlog.contextvars.bound_contextvars(run_id=str(run_id), company_id=company_id):
            log.info("company.done", jobs_found=3)
        log.debug("hidden")  # dưới LOG_LEVEL=INFO
    finally:
        jlog.detach_sink(sink)
        await sink.close()
    [row] = writer.rows
    assert row[1:5] == ("info", "company.done", run_id, company_id)
    assert json.loads(row[5]) == {"jobs_found": 3}
    assert isinstance(row[3], UUID)


@pytest.mark.usefixtures("clean_env")
@pytest.mark.parametrize("fmt", ["console", "json"])
def test_exception_logs_are_cheap_and_hide_locals(
    fmt: str, capsys: pytest.CaptureFixture[str]
) -> None:
    jlog.configure_logging(Settings(_env_file=None, log_format=fmt))  # pyright: ignore[reportCallIssue]
    secret = "mat-khau-proxy-123"  # noqa: F841 — biến cục bộ không được in ra log
    try:
        raise ValueError("boom")
    except ValueError:
        jlog.get_logger("test").exception("poll.error")
    err = capsys.readouterr().err
    assert "ValueError" in err
    assert "boom" in err
    assert "mat-khau-proxy-123" not in err
    assert "╭" not in err  # không phải traceback Rich (tô màu từng frame, chậm hàng giây)
