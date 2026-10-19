"""structlog → stderr: dạng đọc được (console) hoặc JSON mỗi dòng một event (LOG_FORMAT=json)."""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog
from structlog.tracebacks import ExceptionDictTransformer


def _utf8_streams() -> None:
    """Console Windows / stdout bị chuyển hướng hay dùng cp1252: log tiếng Việt làm crash."""
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        reconfigure = getattr(stream, "reconfigure", None)
        if encoding != "utf8" and reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _console_colors() -> bool:
    if not sys.stderr.isatty():
        return False
    if sys.platform == "win32":  # structlog cần colorama để tô màu trên Windows
        try:
            import colorama  # noqa: F401  # pyright: ignore[reportMissingModuleSource]
        except ImportError:
            return False
    return True


def configure_logging(level: str = "INFO", fmt: str = "console") -> None:
    """Cấu hình structlog + logging chuẩn (grpc, asyncio...) dùng chung một renderer."""
    _utf8_streams()
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]
    # Traceback không kèm biến cục bộ: bản Rich mặc định của structlog tốn hàng giây mỗi lần log
    # lỗi (chặn event loop) và locals có thể chứa token / mật khẩu proxy.
    final: list[Any]
    if fmt == "json":
        final = [
            structlog.processors.ExceptionRenderer(ExceptionDictTransformer(show_locals=False)),
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ]
    else:
        final = [
            structlog.dev.ConsoleRenderer(
                colors=_console_colors(), exception_formatter=structlog.dev.plain_traceback
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
    root.setLevel(level.upper())


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)
