"""`jobcrawler serve`: canh các process con của container `backend`."""

from __future__ import annotations

import subprocess
import sys

from jobcrawler.entrypoints.serve import Supervisor, child_commands


class FakeProc:
    def __init__(
        self, command: list[str], *, exit_after: int | None = None, stubborn: bool = False
    ) -> None:
        self.command = command
        self.pid = 1000 + len(command)
        self.code: int | None = None
        self.exit_after = exit_after  # số lần poll() trước khi tự thoát (mã 3)
        self.stubborn = stubborn  # bỏ qua terminate()
        self.polls = 0
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        self.polls += 1
        if self.code is None and self.exit_after is not None and self.polls > self.exit_after:
            self.code = 3
        return self.code

    def terminate(self) -> None:
        self.terminated = True
        if not self.stubborn:
            self.code = -15

    def wait(self, timeout: float | None = None) -> int:
        if self.code is None:
            raise subprocess.TimeoutExpired(self.command, timeout or 0)
        return self.code

    def kill(self) -> None:
        self.killed = True
        self.code = -9


def _supervisor(procs: dict[str, FakeProc], **kw: object) -> Supervisor:
    pending = iter(procs.values())
    return Supervisor(
        {name: [name] for name in procs},
        popen=lambda command: next(pending),
        sleep=lambda s: None,
        **kw,  # pyright: ignore[reportArgumentType]
    )


def test_child_commands() -> None:
    cmds = child_commands("0.0.0.0", 8000)
    assert list(cmds) == ["api", "gateway", "scheduler", "runner"]
    assert cmds["api"][:3] == [sys.executable, "-m", "jobcrawler.entrypoints.cli"]
    assert cmds["api"][3:] == ["api", "--host", "0.0.0.0", "--port", "8000"]
    assert cmds["runner"][3:] == ["runner"]


def test_child_exit_stops_the_others() -> None:
    procs = {"api": FakeProc(["api"]), "runner": FakeProc(["runner"], exit_after=2)}
    code = _supervisor(procs).run()
    assert code == 3
    assert procs["api"].terminated
    assert not procs["runner"].terminated  # đã thoát, không cần dừng


def test_clean_child_exit_still_fails_the_container() -> None:
    proc = FakeProc(["api"])
    proc.code = 0
    assert _supervisor({"api": proc}).run() == 1


def test_stop_request_terminates_children() -> None:
    procs = {"api": FakeProc(["api"]), "gateway": FakeProc(["gateway"])}
    sup = _supervisor(procs)
    sup._sleep = lambda s: sup.request_stop(15)  # pyright: ignore[reportPrivateUsage]
    assert sup.run() == 0
    assert all(p.terminated and not p.killed for p in procs.values())


def test_stubborn_child_is_killed() -> None:
    procs = {"api": FakeProc(["api"], stubborn=True), "runner": FakeProc(["runner"], exit_after=0)}
    assert _supervisor(procs, stop_seconds=0).run() == 3
    assert procs["api"].terminated
    assert procs["api"].killed


class FakeWatch:
    def __init__(self, wanted: int = 0) -> None:
        self.value = wanted
        self.marked: list[int] = []
        self.broken = False

    def wanted(self) -> int:
        if self.broken:
            raise ConnectionError("redis down")
        return self.value

    def applied(self, value: int) -> None:
        if self.broken:
            raise ConnectionError("redis down")
        self.marked.append(value)


def _popen_factory(started: list[FakeProc]):
    def popen(command: list[str]) -> FakeProc:
        proc = FakeProc(command)
        started.append(proc)
        return proc

    return popen


def test_settings_restart_restarts_all_children() -> None:
    started: list[FakeProc] = []
    watch = FakeWatch(wanted=4)  # yêu cầu cũ: process con mới đã đọc cài đặt mới nhất
    ticks = iter(range(100))
    sup = Supervisor(
        {"api": ["api"], "runner": ["runner"]},
        watch=watch,
        popen=_popen_factory(started),
        watch_seconds=0,
        clock=lambda: float(next(ticks)),
    )

    def tick(_s: float) -> None:
        if len(watch.marked) == 2:
            watch.value = 5  # trang Cài đặt lưu một mục live=False
        if sup.restarts == 1:
            sup.request_stop()

    sup._sleep = tick  # pyright: ignore[reportPrivateUsage]
    assert sup.run() == 0
    assert watch.marked[0] == 4
    assert sup.restarts == 1
    assert [p.command for p in started] == [["api"], ["runner"], ["api"], ["runner"]]
    assert started[0].terminated  # bản cũ dừng êm
    assert started[1].terminated
    assert watch.marked[-1] == 5


def test_watch_errors_do_not_stop_serve() -> None:
    started: list[FakeProc] = []
    watch = FakeWatch()
    watch.broken = True
    calls = iter(range(100))
    sup = Supervisor(
        {"api": ["api"]},
        watch=watch,
        popen=_popen_factory(started),
        watch_seconds=0,
        clock=lambda: float(next(calls)),
    )
    rounds = iter(range(5))

    def tick(_s: float) -> None:
        if next(rounds) == 3:
            watch.broken = False  # Redis lên lại: không khởi động lại vô cớ
            watch.value = 7
        if len(watch.marked) >= 1:
            sup.request_stop()

    sup._sleep = tick  # pyright: ignore[reportPrivateUsage]
    assert sup.run() == 0
    assert sup.restarts == 0
    assert watch.marked == [7]
    assert len(started) == 1
