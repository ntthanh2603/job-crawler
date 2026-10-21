"""Log của worker không lộ mật khẩu proxy / token (cùng quy tắc với `jobcrawler.common.logging`)."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

import pytest
import structlog

from jobworker.common import logging as wlog


@pytest.fixture
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    structlog.reset_defaults()
    root.handlers = handlers
    root.setLevel(level)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("http://user:p%40ss@1.2.3.4:8080", "http://user:***@1.2.3.4:8080"),
        ("proxy 'socks5h://u:p@h:1080' lỗi", "proxy 'socks5h://u:***@h:1080' lỗi"),
        (
            "ProxyError: http://u:se@cr@et@proxy.vn:3128 refused",
            "ProxyError: http://u:***@proxy.vn:3128 refused",
        ),
        ("authorization: Bearer abc.DEF-123_x=", "authorization: Bearer ***"),
        (
            "GET https://api.x.vn/search?q=a&api_key=XYZ&num=10",
            "GET https://api.x.vn/search?q=a&api_key=***&num=10",
        ),
        ("https://x.vn/?key=AIza&access_token=t1#f", "https://x.vn/?key=***&access_token=***#f"),
        # Không có gì để che: giữ nguyên.
        ("http://1.2.3.4:8080/a@b", "http://1.2.3.4:8080/a@b"),
        (
            "liên hệ a@b.vn, https://acme.vn:443/jobs?page=2",
            "liên hệ a@b.vn, https://acme.vn:443/jobs?page=2",
        ),
        ("worker.task_done", "worker.task_done"),
    ],
)
def test_redact_text(text: str, expected: str) -> None:
    assert wlog.redact_text(text) == expected


def test_redact_secrets_masks_keys_and_nested_values_without_mutating() -> None:
    headers = {"Authorization": "Basic YWRtaW46cHc=", "Cookie": "sid=1", "Accept": "text/html"}
    plain = {"a": {"b": [1, "x"]}}
    event: dict[str, Any] = {
        "event": "worker.proxy_failed http://u:pw@proxy:8080",
        "gateway_token": "s3cret",
        "password": b"raw",
        "headers": headers,
        "proxies": ["http://a:b@c.vn:1", ("x", "Bearer tok")],
        "plain": plain,
        "local_proxies": 3,
        "has_password": True,
        "token": None,
        "error": ValueError("kết nối http://u:pw@h:1 lỗi"),
        "_record": "http://u:pw@h",  # metadata của logging: không đụng
    }
    out = wlog.redact_secrets(None, "info", event)
    assert out is event
    assert out["event"] == "worker.proxy_failed http://u:***@proxy:8080"
    assert out["gateway_token"] == out["password"] == "***"
    assert out["headers"] == {"Authorization": "***", "Cookie": "***", "Accept": "text/html"}
    assert headers["Authorization"] == "Basic YWRtaW46cHc="  # dict của bên gọi không bị sửa
    assert out["proxies"] == ["http://a:***@c.vn:1", ("x", "Bearer ***")]
    assert out["plain"] is plain  # không có gì để che: không sao chép
    assert (out["local_proxies"], out["has_password"], out["token"]) == (3, True, None)
    assert out["error"] == "ValueError: kết nối http://u:***@h:1 lỗi"
    assert out["_record"] == "http://u:pw@h"


def test_redact_secrets_bounds_depth() -> None:
    loop: list[Any] = ["http://u:pw@h"]
    loop.append(loop)  # tự tham chiếu: không đệ quy vô hạn
    deep: Any = "http://u:pw@h"
    for _ in range(20):
        deep = {"x": deep}
    out = wlog.redact_secrets(None, "info", {"event": "x", "loop": loop, "deep": deep})
    assert out["loop"][0] == "http://u:***@h"
    assert out["deep"] is deep  # quá độ sâu giới hạn: giữ nguyên


@pytest.mark.usefixtures("restore_logging")
@pytest.mark.parametrize("fmt", ["console", "json"])
def test_secrets_never_reach_stderr(fmt: str, capsys: pytest.CaptureFixture[str]) -> None:
    wlog.configure_logging("INFO", fmt)
    log = wlog.get_logger("test")
    log.warning("worker.proxy", proxy="http://user:pw-proxy-1@1.2.3.4:8080", password="pw-2")
    secret = "pw-local-5"  # noqa: F841 — biến cục bộ không được in ra log
    try:
        raise ConnectionError("không nối được socks5://u:pw-exc-3@h:1080")
    except ConnectionError:
        log.exception("worker.task_error", headers={"Authorization": "Bearer pw-token-4"})
    logging.getLogger("grpc").warning("kênh http://g:pw-std-6@gw:50051 lỗi")  # log chuẩn
    err = capsys.readouterr().err
    assert "pw-" not in err
    assert "http://user:***@1.2.3.4:8080" in err
    assert "socks5://u:***@h:1080" in err  # thông điệp lỗi trong traceback
    assert "http://g:***@gw:50051" in err
    assert "ConnectionError" in err
    assert "╭" not in err  # không phải traceback Rich (chậm, in locals)
    if fmt == "json":
        first = json.loads(err.splitlines()[0])
        assert (first["proxy"], first["password"]) == ("http://user:***@1.2.3.4:8080", "***")
