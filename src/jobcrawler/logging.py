"""structlog → terminal (stderr) + Postgres (bảng `logs`).

Ghi Postgres không chặn worker: processor chỉ đẩy event vào `asyncio.Queue`, một task nền
gom và ghi theo lô mỗi giây bằng `copy_records_to_table` của asyncpg. `PgLogSink.close()`
flush hết hàng đợi trước khi đóng.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
import traceback
from collections.abc import Awaitable, Callable, MutableMapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine

from jobcrawler.config import Settings

LOG_COLUMNS = ("ts", "level", "event", "run_id", "company_id", "context")

LogRow = tuple[datetime, str, str, UUID | None, int | None, str]
LogWriter = Callable[[list[LogRow]], Awaitable[None]]

# Các khoá đã có cột riêng hoặc là metadata của structlog/logging, không đưa vào `context`.
_RESERVED_KEYS = frozenset(
    {"timestamp", "level", "event", "run_id", "company_id", "exc_info", "stack_info"}
)
_LEVELS = logging.getLevelNamesMapping()


def _level_no(level: str) -> int:
    return _LEVELS.get(level.upper(), logging.INFO)


def _as_uuid(value: Any) -> UUID | None:
    if value is None or isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except ValueError:
        return None


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _format_exception(exc_info: Any) -> str | None:
    if exc_info is True:
        exc_info = sys.exc_info()
    elif isinstance(exc_info, BaseException):
        exc_info = (type(exc_info), exc_info, exc_info.__traceback__)
    if isinstance(exc_info, tuple) and exc_info[0] is not None:
        return "".join(traceback.format_exception(*exc_info))
    return None


def event_to_row(event_dict: MutableMapping[str, Any]) -> LogRow:
    """Chuyển một event structlog thành một dòng của bảng `logs`."""
    context = {
        k: v for k, v in event_dict.items() if k not in _RESERVED_KEYS and not k.startswith("_")
    }
    if (exc := _format_exception(event_dict.get("exc_info"))) is not None:
        context["exception"] = exc
    return (
        datetime.now(UTC),
        str(event_dict.get("level", "info")),
        str(event_dict.get("event", "")),
        _as_uuid(event_dict.get("run_id")),
        _as_int(event_dict.get("company_id")),
        json.dumps(context, default=str, ensure_ascii=False),
    )


class PgLogSink:
    """Hàng đợi log trong bộ nhớ + task nền ghi theo lô qua `writer`."""

    def __init__(
        self,
        writer: LogWriter,
        *,
        min_level: str = "INFO",
        interval: float = 1.0,
        max_queue: int = 50_000,
        batch_size: int = 5_000,
    ) -> None:
        self._writer = writer
        self._min_level = _level_no(min_level)
        self._interval = interval
        self._batch_size = batch_size
        self._queue: asyncio.Queue[LogRow] = asyncio.Queue(maxsize=max_queue)
        self._stopping = asyncio.Event()
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self.dropped = 0
        self.written = 0

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._task = self._loop.create_task(self._run(), name="pg-log-sink")

    def emit(self, event_dict: MutableMapping[str, Any]) -> None:
        """Gọi từ processor structlog; không bao giờ chặn hay ném lỗi."""
        if _level_no(str(event_dict.get("level", "info"))) < self._min_level:
            return
        loop = self._loop
        if loop is None or loop.is_closed() or self._closed:
            return
        row = event_to_row(event_dict)
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._put(row)
        else:  # log phát ra từ thread khác
            loop.call_soon_threadsafe(self._put, row)

    def _put(self, row: LogRow) -> None:
        try:
            self._queue.put_nowait(row)
        except asyncio.QueueFull:
            self.dropped += 1

    async def _run(self) -> None:
        while not self._stopping.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval)
            await self.flush()

    async def flush(self) -> None:
        while not self._queue.empty():
            batch: list[LogRow] = []
            while len(batch) < self._batch_size and not self._queue.empty():
                batch.append(self._queue.get_nowait())
            try:
                await self._writer(batch)
                self.written += len(batch)
            except Exception as exc:  # không log qua structlog để tránh vòng lặp
                self.dropped += len(batch)
                print(f"[pg-log-sink] ghi {len(batch)} log thất bại: {exc!r}", file=sys.stderr)

    async def close(self) -> None:
        self._stopping.set()
        if self._task is not None:
            await self._task
            self._task = None
        await self.flush()
        self._closed = True
        if self.dropped:
            print(f"[pg-log-sink] đã bỏ {self.dropped} log", file=sys.stderr)


def make_copy_writer(engine: AsyncEngine) -> LogWriter:
    """Writer ghi lô log bằng COPY (asyncpg `copy_records_to_table`)."""

    async def write(rows: list[LogRow]) -> None:
        async with engine.connect() as conn:
            raw = await conn.get_raw_connection()
            driver: Any = raw.driver_connection
            await driver.copy_records_to_table("logs", records=rows, columns=LOG_COLUMNS)

    return write


_sink: PgLogSink | None = None


def attach_sink(sink: PgLogSink) -> None:
    global _sink
    _sink = sink


def detach_sink(sink: PgLogSink) -> None:
    global _sink
    if _sink is sink:
        _sink = None


def _to_pg_sink(_logger: Any, _method: str, event_dict: MutableMapping[str, Any]) -> Any:
    sink = _sink
    if sink is not None:
        try:
            sink.emit(event_dict)
        except Exception as exc:
            print(f"[pg-log-sink] emit lỗi: {exc!r}", file=sys.stderr)
    return event_dict


def _console_colors() -> bool:
    if not sys.stderr.isatty():
        return False
    if sys.platform == "win32":  # structlog cần colorama để tô màu trên Windows
        try:
            import colorama  # noqa: F401  # pyright: ignore[reportMissingModuleSource]
        except ImportError:
            return False
    return True


def configure_logging(settings: Settings) -> None:
    """Cấu hình structlog + logging chuẩn (SAQ, Alembic...) dùng chung một renderer."""
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        _to_pg_sink,
    ]
    renderer: Any
    if settings.log_format == "json":
        renderer = structlog.processors.JSONRenderer(ensure_ascii=False)
        final = [structlog.processors.dict_tracebacks, renderer]
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=_console_colors())
        final = [renderer]

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, *final],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level)
    # openai/httpx log mỗi request ở INFO, alembic log từng plugin; chỉ giữ cảnh báo.
    for noisy in ("httpx", "httpcore", "openai", "alembic.runtime.plugins"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)
