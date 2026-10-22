"""`jobcrawler serve`: canh các process con của container `backend`."""

from __future__ import annotations

import subprocess
import sys

import pytest

from jobcrawler.bootstrap.serve import Supervisor, child_commands


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
    cmds = child_commands("0.0.0.0", 8000, cpus=2)
    assert list(cmds) == ["api", "gateway", "scheduler", "runner"]
    assert cmds["api"][:3] == [sys.executable, "-m", "jobcrawler.bootstrap.cli"]
    assert cmds["api"][3:] == ["api", "--host", "0.0.0.0", "--port", "8000"]
    assert cmds["runner"][3:] == ["runner"]


def test_runner_roles_get_their_own_processes_on_bigger_machines() -> None:
    """Runner một luồng: build / index trang tuyển dụng / poll mỗi vai một process để phần parse
    HTML của lượt build không làm chậm lượt poll."""
    cmds = child_commands("0.0.0.0", 8000, cpus=4)
    assert list(cmds) == [
        "api",
        "gateway",
        "scheduler",
        "runner-build",
        "runner-boards",
        "runner-poll-1",
        "runner-poll-2",
    ]
    assert cmds["runner-build"][3:] == ["runner", "--role", "build"]
    assert cmds["runner-poll-2"][3:] == ["runner", "--role", "poll", "--processes", "2"]


def test_more_cpus_run_more_build_and_poll_processes() -> None:
    """Nhiều worker cho nhiều lượt chạy cùng lúc: mỗi 4 CPU thêm một process build và một process
    poll (tối đa 4 mỗi vai), để phần parse không dồn vào một core."""
    cmds = child_commands("0.0.0.0", 8000, cpus=12)
    builds = [k for k in cmds if k.startswith("runner-build")]
    polls = [k for k in cmds if k.startswith("runner-poll")]
    assert builds == ["runner-build-1", "runner-build-2", "runner-build-3"]
    assert polls == ["runner-poll-1", "runner-poll-2", "runner-poll-3"]
    assert cmds["runner-build-2"][3:] == ["runner", "--role", "build", "--processes", "3"]
    assert len([k for k in child_commands("0.0.0.0", 8000, cpus=64) if "build" in k]) == 4


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


class SeqWatch(FakeWatch):
    """Bộ đếm đọc lần lượt `values` (hết thì giữ số cuối)."""

    def __init__(self, *values: int) -> None:
        super().__init__(values[0])
        self.values = list(values)

    def wanted(self) -> int:
        self.value = self.values.pop(0) if len(self.values) > 1 else self.values[0]
        return super().wanted()


@pytest.mark.parametrize(
    ("values", "restarts"),
    [
        ((5, 0, 1), 1),  # Redis mới: đếm lại từ 0, theo số mới; lần lưu sau đó vẫn khởi động lại
        ((5, 2), 1),  # đếm lại và đã lưu trước khi serve kịp thấy: khởi động lại
        ((5, 0, 0), 0),  # chỉ đếm lại, chưa lưu gì
        ((5, 5, 6), 1),
    ],
)
def test_restart_counter_reset(values: tuple[int, ...], restarts: int) -> None:
    started: list[FakeProc] = []
    watch = SeqWatch(*values)
    ticks = iter(range(100))
    sup = Supervisor(
        {"api": ["api"]},
        watch=watch,
        popen=_popen_factory(started),
        watch_seconds=0,
        clock=lambda: float(next(ticks)),
    )

    def tick(_s: float) -> None:
        if len(watch.marked) >= len(values):
            sup.request_stop()

    sup._sleep = tick  # pyright: ignore[reportPrivateUsage]
    assert sup.run() == 0
    assert watch.marked == list(values)
    assert sup.restarts == restarts
    assert len(started) == 1 + restarts
