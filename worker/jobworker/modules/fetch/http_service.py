"""Việc HTTP: một request qua curl_cffi (impersonate Chrome), trả nguyên văn phản hồi.

Không giữ cookie giữa các việc (`discard_cookies`): cookie cần thiết do backend gửi trong header.
Không đọc biến môi trường HTTP(S)_PROXY (`trust_env=False`): proxy chỉ đến từ việc hoặc pool của
worker. Body vượt `max_body_bytes` bị curl cắt ngay khi tải (CURLOPT_MAXFILESIZE_LARGE) và kiểm lại
sau giải nén.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from jcproto.v1 import crawl_pb2
from jobworker.modules.fetch.results_service import elapsed_ms, encode_body, failure, task_timeout


def classify_error(exc: BaseException, *, proxied: bool = False) -> crawl_pb2.ErrorKind:
    """Lỗi curl_cffi → ErrorKind (giống cách phân loại cũ của backend). Đi qua proxy mà không kết
    nối được thì lỗi là của proxy: kết nối tới đích do proxy làm (lỗi đó về dạng 5xx / CONNECT)."""
    from curl_cffi.const import CurlECode
    from curl_cffi.requests import exceptions as cx

    code = getattr(exc, "code", 0)
    if code == CurlECode.FILESIZE_EXCEEDED:
        return crawl_pb2.ERROR_KIND_TOO_LARGE
    if isinstance(exc, cx.ProxyError | cx.InvalidProxyURL):
        return crawl_pb2.ERROR_KIND_PROXY
    if proxied and code == CurlECode.COULDNT_CONNECT:
        return crawl_pb2.ERROR_KIND_PROXY
    if isinstance(exc, cx.ConnectTimeout):
        return crawl_pb2.ERROR_KIND_CONNECT
    if isinstance(exc, cx.Timeout):
        return crawl_pb2.ERROR_KIND_TIMEOUT
    if isinstance(exc, cx.ConnectionError):
        return crawl_pb2.ERROR_KIND_CONNECT
    return crawl_pb2.ERROR_KIND_OTHER


def certificate_error(exc: BaseException) -> bool:
    """curl không kiểm được chứng chỉ của site (lỗi 60): thường là site thiếu chứng chỉ trung gian
    — trình duyệt tự tải bù nên người xem không thấy lỗi, curl thì không."""
    from curl_cffi.requests import exceptions as cx

    return isinstance(exc, cx.CertificateVerifyError)


@dataclass(slots=True)
class _Session:
    session: Any
    inflight: int = 0
    retired: bool = False


class HttpFetcher:
    """curl_cffi `AsyncSession`, tối đa `max_clients` request cùng lúc (= số slot HTTP)."""

    def __init__(
        self,
        *,
        worker_id: str,
        impersonate: str = "chrome",
        max_clients: int = 32,
        max_body_bytes: int = 10 * 1024 * 1024,
    ) -> None:
        self.worker_id = worker_id
        self.impersonate = impersonate
        self.max_body_bytes = max_body_bytes
        self._max_clients = max_clients
        self._current = _Session(self._new_session(max_clients))
        self._closing: set[asyncio.Task[None]] = set()
        self.bytes_in = 0

    def _new_session(self, max_clients: int) -> Any:
        from curl_cffi import CurlOpt
        from curl_cffi.requests import AsyncSession

        return AsyncSession(
            impersonate=self.impersonate,  # pyright: ignore[reportArgumentType]
            max_clients=max_clients,
            discard_cookies=True,
            trust_env=False,
            curl_options={CurlOpt.MAXFILESIZE_LARGE: self.max_body_bytes},
        )

    def reconfigure(self, *, impersonate: str, max_clients: int, max_body_bytes: int) -> None:
        """Áp cấu hình mới. Đổi `max_clients` thì mở session mới; session cũ đóng khi hết việc."""
        from curl_cffi import CurlOpt

        self.impersonate = impersonate
        self.max_body_bytes = max_body_bytes
        if max_clients != self._max_clients:
            old = self._current
            self._max_clients = max_clients
            self._current = _Session(self._new_session(max_clients))
            old.retired = True
            self._release(old, used=False)
        else:
            self._current.session.curl_options[CurlOpt.MAXFILESIZE_LARGE] = max_body_bytes

    def _release(self, slot: _Session, *, used: bool = True) -> None:
        if used:
            slot.inflight -= 1
        if slot.retired and slot.inflight == 0:
            task = asyncio.ensure_future(slot.session.close())
            self._closing.add(task)
            task.add_done_callback(self._closing.discard)

    async def execute(
        self, task: crawl_pb2.FetchTask, *, proxy: str | None = None, proxy_id: str = ""
    ) -> crawl_pb2.FetchResult:
        started = time.monotonic()
        timeout = task_timeout(task)
        if timeout <= 0:
            return failure(
                task,
                crawl_pb2.ERROR_KIND_EXPIRED,
                "quá hạn trước khi tải",
                worker_id=self.worker_id,
                proxy_id=proxy_id,
            )
        slot = self._current
        slot.inflight += 1
        max_body = self.max_body_bytes

        async def request(timeout: float, *, verify: bool = True) -> Any:
            return await slot.session.request(
                task.method.upper() or "GET",
                task.url,
                headers=dict(task.headers),
                data=task.body or None,
                proxy=proxy,
                timeout=timeout,
                allow_redirects=task.allow_redirects,
                max_redirects=10,
                impersonate=self.impersonate,
                verify=verify,
            )

        try:
            try:
                resp = await request(timeout)
            except Exception as exc:
                left = timeout - (time.monotonic() - started)
                if not certificate_error(exc) or left <= 0:
                    raise
                # Chỉ đọc trang công khai, không gửi gì bí mật: đọc lại không kiểm chứng chỉ thay
                # vì bỏ cả site (robots.txt lỗi → coi như cấm toàn site).
                resp = await request(left, verify=False)
        except Exception as exc:
            kind = classify_error(exc, proxied=bool(proxy))
            message = (
                f"body vượt {max_body} byte"
                if kind == crawl_pb2.ERROR_KIND_TOO_LARGE
                else f"{type(exc).__name__}: {exc}"
            )
            return failure(
                task,
                kind,
                message,
                worker_id=self.worker_id,
                proxy_id=proxy_id,
                duration_ms=elapsed_ms(started),
            )
        finally:
            self._release(slot)

        content: bytes = resp.content
        self.bytes_in += len(content)
        headers = {str(k).lower(): str(v) for k, v in resp.headers.items()}
        if len(content) > max_body:
            result = failure(
                task,
                crawl_pb2.ERROR_KIND_TOO_LARGE,
                f"body {len(content)} byte vượt {max_body} byte",
                worker_id=self.worker_id,
                proxy_id=proxy_id,
                duration_ms=elapsed_ms(started),
                status=resp.status_code,
            )
            result.final_url = str(resp.url)
            result.headers.update(headers)
            return result
        body, zstd = await encode_body(content)
        return crawl_pb2.FetchResult(
            task_id=task.task_id,
            status=resp.status_code,
            final_url=str(resp.url),
            headers=headers,
            body=body,
            body_zstd=zstd,
            proxy_id=proxy_id,
            duration_ms=elapsed_ms(started),
            worker_id=self.worker_id,
        )

    async def close(self) -> None:
        await self._current.session.close()
        if self._closing:
            await asyncio.gather(*self._closing, return_exceptions=True)
