"""`jobcrawler serve`: cả backend trong một container — chạy migration rồi bốn process con `api`,
`gateway`, `scheduler`, `runner` (đủ ba vai). Mỗi process con có event loop và watchdog riêng:
runner bận parse HTML không làm chậm API.

Process con nào thoát thì dừng các process còn lại và thoát theo mã của nó (0 → 1), để Docker
(`restart: unless-stopped`) dựng lại cả container. SIGTERM / Ctrl+C: gửi SIGTERM cho các process
con, chờ chúng dừng tối đa STOP_SECONDS rồi giết nốt.

Cài đặt `live=False` (trang Cài đặt): lưu xong thì bộ đếm `<REDIS_PREFIX>:settings:restart` tăng
(`modules.settings.app_settings_service`); mỗi WATCH_SECONDS serve đọc bộ đếm, tăng thì khởi động
lại mọi process con (dừng êm như trên) rồi ghi số đã áp dụng vào `...:settings:restart_applied`.
Bộ đếm nhỏ đi (Redis mới / bị xoá, đếm lại từ 0) thì serve theo số mới; số đó > 0 (đã có lần lưu
sau khi đếm lại) thì cũng khởi động lại.
Serve cũng giữ khoá `<REDIS_PREFIX>:serve:alive` (TTL ALIVE_TTL_SECONDS) để trang Cài đặt biết cài
đặt được tự áp dụng. Mất Redis không làm serve thoát (chỉ log, thử lại ở nhịp sau)."""

from __future__ import annotations

import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from types import FrameType
from typing import Any, Protocol

from jobcrawler.common.logging import get_logger
from jobcrawler.modules.settings import app_settings_service

log = get_logger(__name__)

POLL_SECONDS = 0.5
# Nhỏ hơn stop_grace_period của container (30 s) để kịp giết process con không chịu dừng.
STOP_SECONDS = 25.0
WATCH_SECONDS = 2.0
ALIVE_TTL_SECONDS = 15


def child_commands(host: str, port: int) -> dict[str, list[str]]:
    """Lệnh của từng process con (cùng trình thông dịch, cùng biến môi trường)."""
    cli = [sys.executable, "-m", "jobcrawler.bootstrap.cli"]
    return {
        "api": [*cli, "api", "--host", host, "--port", str(port)],
        "gateway": [*cli, "gateway"],
        "scheduler": [*cli, "scheduler"],
        "runner": [*cli, "runner"],
    }


class Watch(Protocol):
    """Nguồn yêu cầu khởi động lại (bộ đếm tăng dần)."""

    def wanted(self) -> int: ...

    def applied(self, value: int) -> None: ...


class RestartWatch:
    """Bộ đếm khởi động lại của trang Cài đặt trên Redis (client đồng bộ của redis-py)."""

    def __init__(self, redis: Any, prefix: str) -> None:
        self._redis = redis
        self._prefix = prefix

    def wanted(self) -> int:
        return int(self._redis.get(app_settings_service.restart_key(self._prefix)) or 0)

    def applied(self, value: int) -> None:
        pipe = self._redis.pipeline()
        pipe.set(app_settings_service.restart_applied_key(self._prefix), value)
        pipe.set(app_settings_service.supervisor_key(self._prefix), value, ex=ALIVE_TTL_SECONDS)
        pipe.execute()


class Supervisor:
    """Chạy và canh các process con; xem docstring của module."""

    def __init__(
        self,
        commands: Mapping[str, Sequence[str]],
        *,
        watch: Watch | None = None,
        popen: Callable[[list[str]], Any] = subprocess.Popen,
        poll_seconds: float = POLL_SECONDS,
        stop_seconds: float = STOP_SECONDS,
        watch_seconds: float = WATCH_SECONDS,
        sleep: Callable[[float], object] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._commands = commands
        self._watch = watch
        self._popen = popen
        self._poll_seconds = poll_seconds
        self._stop_seconds = stop_seconds
        self._watch_seconds = watch_seconds
        self._sleep = sleep
        self._clock = clock
        self._stopping = False
        self._watch_ok = True
        # None: chưa đọc được bộ đếm từ lúc chạy process con (Redis chưa lên).
        self._applied: int | None = None
        self.procs: dict[str, Any] = {}
        self.restarts = 0

    def request_stop(self, signum: int | None = None, _frame: FrameType | None = None) -> None:
        if not self._stopping:
            log.info("serve.stopping", signal=signum)
        self._stopping = True

    def run(self) -> int:
        """Chạy tới khi được yêu cầu dừng (trả 0) hoặc một process con thoát (trả mã của nó)."""
        try:
            # Process con mới đọc cài đặt mới nhất: mọi yêu cầu khởi động lại tới giờ đã áp dụng.
            wanted = self._read_watch()
            self._start_all()
            if wanted is not None:
                self._mark_applied(wanted)
            next_watch = self._clock() + self._watch_seconds
            while not self._stopping:
                for name, proc in self.procs.items():
                    code = proc.poll()
                    if code is not None and not self._stopping:
                        log.error("serve.child_exit", child=name, code=code)
                        return code or 1
                if self._clock() >= next_watch:
                    next_watch = self._clock() + self._watch_seconds
                    self._check_watch()
                self._sleep(self._poll_seconds)
            return 0
        finally:
            self._stop_all()

    def _start_all(self) -> None:
        for name, command in self._commands.items():
            self.procs[name] = self._popen(list(command))
            log.info("serve.started", child=name, pid=self.procs[name].pid)

    def _check_watch(self) -> None:
        wanted = self._read_watch()
        if wanted is None:
            return
        applied = self._applied
        if applied is not None and wanted < applied:
            # Bộ đếm trên Redis về lại từ đầu (Redis mới / bị xoá): theo số mới, không thì N lần lưu
            # tiếp theo không khởi động lại. Số > 0 nghĩa là đã có lần lưu sau đó: khởi động lại.
            log.warning("serve.restart_counter_reset", wanted=wanted, applied=applied)
        if applied is not None and wanted != applied and wanted > 0:
            log.info("serve.restart_for_settings", wanted=wanted, applied=applied)
            self._stop_all()
            if self._stopping:
                return
            self._start_all()
            self.restarts += 1
        self._mark_applied(wanted)

    def _read_watch(self) -> int | None:
        if self._watch is None:
            return None
        try:
            wanted = self._watch.wanted()
        except Exception as exc:
            self._watch_failed(exc)
            return None
        return wanted

    def _mark_applied(self, value: int) -> None:
        self._applied = value
        if self._watch is None:
            return
        try:
            self._watch.applied(value)
        except Exception as exc:
            self._watch_failed(exc)
            return
        if not self._watch_ok:
            log.info("serve.watch_ok")
        self._watch_ok = True

    def _watch_failed(self, exc: Exception) -> None:
        # Chỉ log lần đầu của một chuỗi lỗi (Redis mất lâu không làm đầy log).
        if self._watch_ok:
            log.warning("serve.watch_error", error=str(exc))
        self._watch_ok = False

    def _stop_all(self) -> None:
        alive = {name: p for name, p in self.procs.items() if p.poll() is None}
        for proc in alive.values():
            proc.terminate()  # SIGTERM (POSIX); Windows: TerminateProcess
        deadline = time.monotonic() + self._stop_seconds
        for name, proc in alive.items():
            try:
                proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                log.warning("serve.kill", child=name)
                proc.kill()
                proc.wait()


async def _prepare(*, demo: bool) -> None:
    """Trước khi chạy process con: mã hoá các trường nhạy cảm còn lưu nguyên văn / bằng khoá cũ,
    và (DEMO_COMPANIES) nạp doanh nghiệp mẫu khi DB chưa có doanh nghiệp nào. Lỗi chỉ ghi log."""
    from jobcrawler.modules.companies.companies_service import seed_demo_if_empty
    from jobcrawler.modules.settings import app_settings_service
    from jobcrawler.services.resources import open_resources

    try:
        # Nạp cả cài đặt trong DB (như process con) để log khởi động không báo nhầm thiếu LLM.
        async with open_resources(db_logs=False) as res:
            await app_settings_service.seal_stored(res.sessionmaker)
            outcome = await seed_demo_if_empty(res) if demo else None
    except Exception as exc:
        log.warning("serve.prepare_failed", error=str(exc))
        return
    if outcome is not None:
        log.info("serve.demo_seeded", created=outcome.created, rows=outcome.rows)


def run_serve(host: str, port: int, *, migrate: bool) -> int:
    import asyncio

    from redis import Redis

    from jobcrawler.common.config import get_settings

    if migrate:
        from jobcrawler.database import migrate as db_migrate

        db_migrate.upgrade("head")
    settings = get_settings()
    asyncio.run(_prepare(demo=settings.demo_companies))
    redis = Redis.from_url(settings.redis_url, socket_connect_timeout=3, socket_timeout=3)
    supervisor = Supervisor(
        child_commands(host, port), watch=RestartWatch(redis, settings.redis_prefix)
    )
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, supervisor.request_stop)
    try:
        return supervisor.run()
    finally:
        redis.close()
