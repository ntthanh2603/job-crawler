"""`Transport` / `Renderer` của `HttpClient` gửi từng request tới worker lấy trang (`FetchClient`).

Một request = một `FetchTask`. Proxy: backend chỉ định (PROXY_MODE=central, hoặc kiểm tra proxy)
thì ghi vào `task.proxy`; không thì worker tự chọn proxy của mình (PROXY_MODE=worker) hoặc đi
thẳng (`direct`, PROXY_MODE=off).

Worker giữ việc mà rời / chết (`ERROR_KIND_WORKER_LOST`, gateway báo): gửi lại ngay
LOST_RESUBMITS lần (cả việc render, vốn không được `HttpClient` thử lại); vẫn mất thì là
`TransportError("other", "worker_lost: ...")`, `HttpClient.fetch` thử lại như lỗi kết nối nhưng
không tính lỗi cho proxy.
"""

from __future__ import annotations

import json as jsonlib
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode

import zstandard

from jcproto.v1 import crawl_pb2
from jobcrawler.gateway.dispatch import FetchClient
from jobcrawler.logging import get_logger
from jobcrawler.net.http import (
    CapturedJson,
    RawResponse,
    TransportError,
    TransportErrorKind,
    WaitUntil,
)
from jobcrawler.net.proxy_pool import Proxy, ProxyParseError, parse_proxy_line

log = get_logger(__name__)

_ERROR_KINDS: dict[int, TransportErrorKind] = {
    crawl_pb2.ERROR_KIND_TIMEOUT: "timeout",
    crawl_pb2.ERROR_KIND_PROXY: "proxy",
    crawl_pb2.ERROR_KIND_CONNECT: "connect",
}
MIN_BODY_LIMIT = 64 * 1024 * 1024
LOST_RESUBMITS = 1


def encode_body(
    headers: Mapping[str, str], data: Any = None, json: Any = None
) -> tuple[dict[str, str], bytes]:
    """Body request như curl_cffi / requests: `json` → JSON gọn UTF-8, `data` dict / list cặp →
    form urlencoded, bytes / str → nguyên văn. Thêm Content-Type nếu chưa có."""
    out = dict(headers)
    has_type = any(k.lower() == "content-type" for k in out)
    if json is not None:
        body = jsonlib.dumps(json, ensure_ascii=False, separators=(",", ":")).encode()
        content_type = "application/json"
    elif data is None:
        return out, b""
    elif isinstance(data, (bytes, bytearray)):
        return out, bytes(data)
    elif isinstance(data, str):
        return out, data.encode()
    else:
        body = urlencode(data, doseq=True).encode()
        content_type = "application/x-www-form-urlencoded"
    if not has_type:
        out["Content-Type"] = content_type
    return out, body


def proxy_id_of(url: str | None) -> str:
    """Id của proxy (như `ProxyPool`) từ URL; rỗng nếu không có / không đọc được."""
    if not url:
        return ""
    try:
        proxy = parse_proxy_line(url)
    except ProxyParseError:
        return ""
    return proxy.id if proxy else ""


def _error_name(value: int) -> str:
    try:
        return crawl_pb2.ErrorKind.Name(value).removeprefix("ERROR_KIND_").lower()  # pyright: ignore[reportArgumentType]
    except ValueError:
        return f"error_{value}"


def _unzstd(body: bytes, limit: int) -> bytes:
    try:
        # Frame ghi sẵn kích thước thì `max_output_size` không có tác dụng: tự kiểm tra trước.
        size = zstandard.frame_content_size(body)
        if size > limit:
            raise TransportError("other", f"too_large: body giải nén {size} byte > {limit}")
        return zstandard.ZstdDecompressor().decompress(body, max_output_size=limit)
    except zstandard.ZstdError as exc:
        raise TransportError("other", f"zstd: {exc}") from exc


def _request_json(value: str) -> Any:
    if not value:
        return None
    try:
        return jsonlib.loads(value)
    except ValueError:
        return value


def to_raw_response(
    result: crawl_pb2.FetchResult, url: str, *, max_body: int = MIN_BODY_LIMIT
) -> RawResponse:
    """`FetchResult` của worker → `RawResponse`; lỗi truyền tải → `TransportError`."""
    worker = f" (worker {result.worker_id})" if result.worker_id else ""
    if result.error_kind != crawl_pb2.ERROR_KIND_NONE:
        kind = _ERROR_KINDS.get(result.error_kind, "other")
        message = result.error or _error_name(result.error_kind)
        if kind == "other" and result.error_kind != crawl_pb2.ERROR_KIND_OTHER:
            message = f"{_error_name(result.error_kind)}: {message}"
        raise TransportError(kind, message + worker)
    if result.status <= 0:
        raise TransportError("other", f"worker trả kết quả không có mã HTTP{worker}")
    content = _unzstd(result.body, max_body) if result.body_zstd else result.body
    captured = tuple(
        CapturedJson(
            url=c.url,
            method=c.method or "GET",
            status=c.status,
            body=c.body,
            request_json=_request_json(c.request_json),
        )
        for c in result.captured
    )
    return RawResponse(
        status=result.status,
        url=result.final_url or url,
        headers={k.lower(): v for k, v in result.headers.items()},
        content=content,
        captured=captured,
        proxy_id=result.proxy_id or None,
    )


class _Remote:
    def __init__(self, client: FetchClient, *, direct: bool = False) -> None:
        """`direct`: worker đi thẳng, không dùng proxy của mình (PROXY_MODE=off) khi việc không
        chỉ định proxy."""
        self.client = client
        self.direct = direct
        # Chặn body giải nén bất thường (worker đã giới hạn FETCH_MAX_BODY_BYTES).
        self._max_body = max(4 * client.max_body_bytes, MIN_BODY_LIMIT)

    async def _run(self, task: crawl_pb2.FetchTask) -> RawResponse:
        task.direct = self.direct and not task.proxy
        result = await self.client.submit(task)
        for _ in range(LOST_RESUBMITS):
            if result.error_kind != crawl_pb2.ERROR_KIND_WORKER_LOST:
                break
            # Việc chưa chạy xong ở worker đã mất: gửi lại (task_id, deadline mới) ngay.
            log.info("fetch.worker_lost", url=task.url, worker_id=result.worker_id)
            result = await self.client.submit(task)
        return to_raw_response(result, task.url, max_body=self._max_body)


class RemoteTransport(_Remote):
    """`Transport` qua worker (curl_cffi tại worker). `close()` không đóng `FetchClient`."""

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        data: Any = None,
        json: Any = None,
        proxy: str | None,
        timeout: float,
        allow_redirects: bool,
    ) -> RawResponse:
        task_headers, body = encode_body(headers, data, json)
        task = crawl_pb2.FetchTask(
            kind=crawl_pb2.TASK_KIND_HTTP,
            method=method.upper(),
            url=url,
            headers=task_headers,
            body=body,
            proxy=proxy or "",
            proxy_id=proxy_id_of(proxy),
            timeout_seconds=timeout,
            allow_redirects=allow_redirects,
        )
        return await self._run(task)

    async def close(self) -> None:
        """`FetchClient` dùng chung, do `Resources` đóng."""


class RemoteRenderer(_Remote):
    """`Renderer` qua worker (Chromium tại worker)."""

    async def render(
        self,
        url: str,
        *,
        proxy: Proxy | None,
        timeout: float,
        wait_until: WaitUntil,
        capture_json: bool = False,
    ) -> RawResponse:
        task = crawl_pb2.FetchTask(
            kind=crawl_pb2.TASK_KIND_RENDER,
            method="GET",
            url=url,
            proxy=proxy.url if proxy else "",
            proxy_id=proxy.id if proxy else "",
            timeout_seconds=timeout,
            allow_redirects=True,
            wait_until=wait_until,
            capture_json=capture_json,
        )
        return await self._run(task)
