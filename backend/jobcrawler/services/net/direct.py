"""`Transport` gọi thẳng từ backend bằng httpx (HTTP/1.1), chỉ cho API bên ngoài (SERP).

Không dùng để crawl: website / ATS / trang tuyển dụng đi qua worker lấy trang
(`modules.gateway.remote_service`). Tham số `proxy` bị bỏ qua: API bên ngoài gọi thẳng, không qua
proxy."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from jobcrawler.services.net.http import RawResponse, TransportError


def _message(exc: Exception) -> str:
    return str(exc) or type(exc).__name__


class DirectTransport:
    def __init__(
        self,
        *,
        max_connections: int = 50,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """`transport`: thay tầng mạng của httpx (test dùng `httpx.MockTransport`)."""
        self._client = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=max_connections),
            transport=transport,
            follow_redirects=False,
        )

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
        body: dict[str, Any] = {}
        if json is not None:
            body["json"] = json
        elif isinstance(data, (bytes, bytearray, str)):
            body["content"] = data
        elif data is not None:
            body["data"] = data
        try:
            resp = await self._client.request(
                method,
                url,
                headers=dict(headers),
                timeout=timeout,
                follow_redirects=allow_redirects,
                **body,
            )
        except httpx.ProxyError as exc:
            raise TransportError("proxy", _message(exc)) from exc
        except httpx.ConnectTimeout as exc:
            raise TransportError("connect", _message(exc)) from exc
        except httpx.TimeoutException as exc:
            raise TransportError("timeout", _message(exc)) from exc
        except httpx.ConnectError as exc:
            raise TransportError("connect", _message(exc)) from exc
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise TransportError("other", _message(exc)) from exc
        return RawResponse(
            status=resp.status_code,
            url=str(resp.url),
            headers={k.lower(): v for k, v in resp.headers.items()},
            content=resp.content,
        )

    async def close(self) -> None:
        await self._client.aclose()
