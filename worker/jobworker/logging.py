"""structlog → stderr: dạng đọc được (console) hoặc JSON mỗi dòng một event (LOG_FORMAT=json).

Mọi event đi qua `redact_secrets` trước renderer: mật khẩu trong URL (proxy tại worker hay đi kèm
`user:pass@`), token `Bearer`, tham số bí mật trong query string và giá trị của khoá có tên giống bí
mật (`password`, `token`, `authorization`...) được thay bằng `***`, kể cả trong thông điệp lỗi của
traceback. Cùng quy tắc với `jobcrawler.logging` của backend (worker không phụ thuộc jobcrawler).
"""

from __future__ import annotations

import io
import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any, TextIO

import structlog
from structlog.tracebacks import ExceptionDictTransformer

REDACTED = "***"
# Khoá có giá trị là bí mật: che cả giá trị. Số / bool / None giữ nguyên (`has_password=True`,
# `local_proxies=3` không phải bí mật).
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
    trong event có thể là object đang dùng của bên gọi (vd. headers của việc).
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
    """Sau `ExceptionRenderer`: che bí mật trong thông điệp lỗi của traceback dạng dict."""
    if "exception" in event_dict:
        event_dict["exception"] = _redact(event_dict["exception"], 0)
    return event_dict


def _plain_traceback(sio: TextIO, exc_info: Any) -> None:
    """`structlog.dev.plain_traceback` (nhanh, không locals) rồi che bí mật trong thông điệp lỗi."""
    buf = io.StringIO()
    structlog.dev.plain_traceback(buf, exc_info)
    sio.write(redact_text(buf.getvalue()))


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
        # Trước renderer: mật khẩu proxy / token không tới stderr.
        redact_secrets,
    ]
    # Traceback không kèm biến cục bộ: bản Rich mặc định của structlog tốn hàng giây mỗi lần log
    # lỗi (chặn event loop) và locals có thể chứa token / mật khẩu proxy.
    final: list[Any]
    if fmt == "json":
        final = [
            structlog.processors.ExceptionRenderer(ExceptionDictTransformer(show_locals=False)),
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
    root.setLevel(level.upper())


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)
