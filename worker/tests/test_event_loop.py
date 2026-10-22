"""Event loop của test (`worker_fakes.GrpcSafeEventLoop`, gắn qua conftest): sự kiện gRPC tới muộn,
sau khi loop của test trước đã đóng, không được giết thread poller chung của grpc.aio."""

from __future__ import annotations

import asyncio

from worker_fakes import GrpcSafeEventLoop  # pyright: ignore[reportMissingImports]


async def test_async_tests_run_on_grpc_safe_loop() -> None:
    assert isinstance(asyncio.get_running_loop(), GrpcSafeEventLoop)


def test_closed_loop_drops_threadsafe_callback() -> None:
    loop = GrpcSafeEventLoop()
    loop.close()
    called: list[int] = []
    handle = loop.call_soon_threadsafe(called.append, 1)  # loop thường: `Event loop is closed`
    assert handle.cancelled()
    assert called == []
