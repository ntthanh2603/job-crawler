"""structlog → terminal (stderr) + Postgres (bảng `logs`).

Ghi Postgres không chặn worker: processor chỉ đẩy event vào `asyncio.Queue`, một task nền
gom và ghi theo lô mỗi giây bằng `copy_records_to_table` của asyncpg. `PgLogSink.close()`
flush hết hàng đợi trước khi đóng.

Mọi event đi qua `redact_secrets` trước khi tới sink và renderer: mật khẩu trong URL, token
`Bearer`, tham số bí mật trong query string và giá trị của khoá có tên giống bí mật (`password`,
`api_key`, `authorization`...) được thay bằng `***` — ở stderr lẫn bảng `logs`.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import re
import sys
import traceback
from collections.abc import Awaitable, Callable, MutableMapping
from datetime import UTC, datetime
from typing import Any, TextIO
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine
from structlog.tracebacks import ExceptionDictTransformer

from jobcrawler.common.config import Settings
from jobcrawler.database.ids import uuid7

# `id` sinh lúc ghi event (UUIDv7 tăng chặt trong process): ORDER BY id đúng thứ tự sự kiện.
LOG_COLUMNS = ("ts", "level", "event", "run_id", "company_id", "context", "id")

LogRow = tuple[datetime, str, str, UUID | None, UUID | None, str, UUID]
LogWriter = Callable[[list[LogRow]], Awaitable[None]]

# Các khoá đã có cột riêng hoặc là metadata của structlog/logging, không đưa vào `context`.
_RESERVED_KEYS = frozenset(
    {"timestamp", "level", "event", "run_id", "company_id", "exc_info", "stack_info"}
)
_LEVELS = logging.getLevelNamesMapping()
# Traceback không kèm biến cục bộ. Mặc định của structlog (Rich: tô màu mã nguồn từng frame + in
# locals) tốn 1–5 s mỗi lần log lỗi và chạy đồng bộ trên event loop: một loạt lỗi làm treo mọi job
# của process (đo được: poll lỗi nối tiếp nhau mỗi ~5 s, các poll khác quá timeout). Locals còn có
# thể chứa token / mật khẩu.
JSON_TRACEBACKS = structlog.processors.ExceptionRenderer(
    ExceptionDictTransformer(show_locals=False)
)

REDACTED = "***"
# Khoá có giá trị là bí mật: che cả giá trị. Số / bool / None giữ nguyên (`prompt_tokens=120`,
# `has_password=True` không phải bí mật).
_SECRET_KEY = re.compile(
    r"secret|token|passw(?:or)?d|api[_-]?key|authorization|cookie|credential", re.IGNORECASE
)
# `scheme://user:pass@host` → `scheme://user:***@host`. Khớp từ `://` (không khớp scheme) để chuỗi
# dài không có URL không tốn thời gian quay lui.
_URL_CREDENTIALS = re.compile(r"(://[^\s:/@'\"<>]*:)[^\s/'\"<>]+@")
# `?api_key=...`, `&access_token=...`, `&key=...` trong URL.
_QUERY_SECRET = re.compile(
    r"([?&;](?:[\w.-]*?(?:secret|token|passw(?:or)?d|api[_-]?key|signature)|key|auth)=)"
    r"[^&\s'\"<>#]+",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(\bbearer\s+)[\w\-.~+/]+=*", re.IGNORECASE)
# Độ sâu tối đa khi duyệt dict/list lồng nhau trong event (sâu hơn thì giữ nguyên).
_REDACT_DEPTH = 6
# Metadata của structlog / logging, không phải nội dung log (khoá `_...` như `_record` cũng bỏ qua).
_REDACT_SKIP = frozenset({"exc_info", "stack_info", "level", "timestamp"})


def redact_text(text: str) -> str:
    """Che bí mật trong một chuỗi: mật khẩu trong URL, tham số bí mật của query, token Bearer."""
    if "://" in text and "@" in text:
        text = _URL_CREDENTIALS.sub(rf"\1{REDACTED}@", text)
    if "=" in text:
        text = _QUERY_SECRET.sub(rf"\1{REDACTED}", text)
    if "earer" in text or "EARER" in text:
        text = _BEARER.sub(rf"\1{REDACTED}", text)
    return text


def _is_secret_key(key: Any, value: Any) -> bool:
    if not isinstance(key, str) or not _SECRET_KEY.search(key):
        return False
    return not (value is None or value == REDACTED or isinstance(value, bool | int | float))


def _redact(value: Any, depth: int) -> Any:
    """Bản đã che của `value`; trả chính `value` khi không có gì để che (không sao chép).

    Dict / list / tuple chỉ được sao chép nông khi có phần tử đổi, không sửa tại chỗ: dict lồng
    trong event có thể là object đang dùng của bên gọi (vd. headers của request).
    """
    if isinstance(value, str):
        return redact_text(value)
    if depth >= _REDACT_DEPTH:
        return value
    if isinstance(value, dict):
        mapping: dict[Any, Any] = value  # pyright: ignore[reportUnknownVariableType]
        changed: dict[Any, Any] | None = None
        for key, item in mapping.items():
            new = REDACTED if _is_secret_key(key, item) else _redact(item, depth + 1)
            if new is not item:
                if changed is None:
                    changed = dict(mapping)
                changed[key] = new
        return mapping if changed is None else changed
    if isinstance(value, list | tuple):
        seq: list[Any] | tuple[Any, ...] = value  # pyright: ignore[reportUnknownVariableType]
        items: list[Any] | None = None
        for i, item in enumerate(seq):
            new = _redact(item, depth + 1)
            if new is not item:
                if items is None:
                    items = list(seq)
                items[i] = new
        if items is None:
            return seq
        return items if isinstance(seq, list) else tuple(items)
    if isinstance(value, BaseException):
        text = str(value)
        masked = redact_text(text)
        return value if masked == text else f"{type(value).__name__}: {masked}"
    return value


def redact_secrets(_logger: Any, _method: str, event_dict: MutableMapping[str, Any]) -> Any:
    """Processor structlog: che bí mật trong mọi giá trị của event (xem `redact_text`)."""
    for key, value in event_dict.items():
        if key in _REDACT_SKIP or key.startswith("_"):
            continue
        new = REDACTED if _is_secret_key(key, value) else _redact(value, 0)
        if new is not value:
            event_dict[key] = new  # chỉ thay giá trị của khoá có sẵn: vẫn duyệt tiếp được
    return event_dict


def _redact_exception(_logger: Any, _method: str, event_dict: MutableMapping[str, Any]) -> Any:
    """Sau `JSON_TRACEBACKS`: che bí mật trong thông điệp lỗi của traceback dạng dict."""
    if "exception" in event_dict:
        event_dict["exception"] = _redact(event_dict["exception"], 0)
    return event_dict


def _plain_traceback(sio: TextIO, exc_info: Any) -> None:
    """`structlog.dev.plain_traceback` (nhanh, không locals) rồi che bí mật trong thông điệp lỗi."""
    buf = io.StringIO()
    structlog.dev.plain_traceback(buf, exc_info)
    sio.write(redact_text(buf.getvalue()))


def _level_no(level: str) -> int:
    return _LEVELS.get(level.upper(), logging.INFO)


def _as_uuid(value: Any) -> UUID | None:
    if value is None or isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except ValueError:
        return None


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
        context["exception"] = redact_text(exc)
    return (
        datetime.now(UTC),
        str(event_dict.get("level", "info")),
        str(event_dict.get("event", "")),
        _as_uuid(event_dict.get("run_id")),
        _as_uuid(event_dict.get("company_id")),
        json.dumps(context, default=str, ensure_ascii=False),
        uuid7(),
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


def _utf8_streams() -> None:
    """Console Windows / stdout bị chuyển hướng hay dùng cp1252: log tiếng Việt làm crash."""
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        if encoding != "utf8" and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")  # pyright: ignore[reportAttributeAccessIssue]


def configure_logging(settings: Settings) -> None:
    """Cấu hình structlog + logging chuẩn (Alembic, uvicorn, grpc...) dùng chung một renderer."""
    _utf8_streams()
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        # Trước sink và renderer: bí mật không tới stderr lẫn bảng `logs`.
        redact_secrets,
        _to_pg_sink,
    ]
    final: list[Any]
    if settings.log_format == "json":
        final = [
            JSON_TRACEBACKS,
            _redact_exception,
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ]
    else:
        final = [
            structlog.dev.ConsoleRenderer(
                colors=_console_colors(), exception_formatter=_plain_traceback
            )
        ]

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
    for noisy in ("httpx", "httpx2", "httpcore", "openai", "alembic.runtime.plugins"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)
