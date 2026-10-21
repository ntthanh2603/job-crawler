"""Build / poll không cần DB (modules/crawl/crawl_service.py, company_service.py): cờ tin mới trên
trang tuyển dụng khi lượt poll `board` bị xếp trùng, LLM chỉ hỏi việc đã gộp."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from jobcrawler.common.config import Settings
from jobcrawler.common.schemas import EmploymentType, JobLevel, RunTrigger, WorkMode
from jobcrawler.modules.crawl import company_service, crawl_service, sla_service
from jobcrawler.pipeline import normalize, store, verify
from jobcrawler.pipeline.normalize import NormalizedJob
from jobcrawler.pipeline.verify import Verdict

from .jobs_fixtures import raw_job

pytestmark = pytest.mark.usefixtures("clean_env")


class FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.ops: list[tuple[str, object]] = []

    async def __aenter__(self) -> FakePipeline:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def set(self, key: str, value: object, ex: int | None = None) -> FakePipeline:
        self.ops.append((key, value))
        return self

    async def execute(self) -> list[bool]:
        for key, value in self.ops:
            self.redis.data[key] = value
        return [True] * len(self.ops)


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, object] = {}

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        return FakePipeline(self)

    async def getdel(self, key: str) -> object | None:
        return self.data.pop(key, None)

    async def exists(self, key: str) -> int:
        return int(key in self.data)


class FakeQueue:
    """Hàng đợi giả: job cùng khoá đang chờ / chạy thì không xếp (như `streamqueue._ENQUEUE`)."""

    def __init__(self, busy: set[str] | None = None) -> None:
        self.busy = set(busy or ())
        self.jobs: list[tuple[str, dict[str, Any]]] = []

    async def enqueue(self, function: str, **kwargs: Any) -> object | None:
        if kwargs["key"] in self.busy:
            return None
        self.busy.add(kwargs["key"])
        self.jobs.append((function, kwargs))
        return object()


class FakeSession:
    async def execute(self, _stmt: object) -> None:
        return None


class FakeSessionmaker:
    @asynccontextmanager
    async def begin(self) -> AsyncIterator[FakeSession]:
        yield FakeSession()


def resources(queue: FakeQueue | None = None) -> Any:
    return SimpleNamespace(
        settings=Settings(),
        redis=FakeRedis(),
        queues=SimpleNamespace(poll=queue or FakeQueue()),
        sessionmaker=FakeSessionmaker(),
        llm=None,
        cache_kv=None,
    )


@pytest.fixture
def no_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    @asynccontextmanager
    async def held(*_args: Any, **_kwargs: Any) -> AsyncIterator[bool]:
        yield True

    async def no_metrics(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(crawl_service, "company_lock", held)
    monkeypatch.setattr(sla_service, "record_poll", no_metrics)


async def test_board_poll_deduped_by_a_waiting_poll_leaves_a_flag() -> None:
    cid = uuid4()
    queue = FakeQueue({crawl_service.poll_key(cid)})  # lượt poll theo lịch đang chờ / chạy
    res = resources(queue)
    assert await crawl_service.enqueue_polls(res, [cid], trigger=RunTrigger.BOARD) == 0
    # Lượt poll đang chờ lấy cờ khi chạy: đọc tin trang tuyển dụng như lượt `board`.
    assert await crawl_service.take_board_pending(res, cid)
    assert not await crawl_service.take_board_pending(res, cid)
    # Poll theo lịch không đặt cờ.
    assert await crawl_service.enqueue_polls(res, [uuid4()], trigger=RunTrigger.SCHEDULE) == 1
    assert res.redis.data == {}


async def test_poll_reads_the_flag_and_follows_up_on_a_new_one(
    monkeypatch: pytest.MonkeyPatch, no_lock: None
) -> None:
    boards: list[bool] = []

    async def fake_poll(res: Any, company_id: UUID, _trigger: Any, board: bool, *_: Any) -> Any:
        boards.append(board)
        # Index thấy thêm tin mới trong lúc lượt này chạy (sau bước đọc cờ).
        await crawl_service.mark_board_pending(res, [company_id])
        return crawl_service.PollOutcome(company_id=company_id, status="ok")

    monkeypatch.setattr(crawl_service, "_poll", fake_poll)
    cid = uuid4()
    queue = FakeQueue({crawl_service.poll_key(cid)})  # khoá `poll:<id>` là chính job đang chạy
    res = resources(queue)
    await crawl_service.mark_board_pending(res, [cid])
    await crawl_service.poll_company(res, cid, trigger=RunTrigger.SCHEDULE)
    assert boards == [True]
    [(function, kwargs)] = queue.jobs
    assert function == "poll_company"
    assert (kwargs["key"], kwargs["trigger"]) == (f"poll:{cid}:board", "board")
    assert await crawl_service.take_board_pending(res, cid)  # lượt xếp thêm sẽ đọc


async def test_failed_poll_keeps_the_flag_without_polling_again_at_once(
    monkeypatch: pytest.MonkeyPatch, no_lock: None
) -> None:
    async def broken_poll(*_args: Any) -> Any:
        raise RuntimeError("index lỗi")

    monkeypatch.setattr(crawl_service, "_poll", broken_poll)
    cid = uuid4()
    queue = FakeQueue()
    res = resources(queue)
    outcome = await crawl_service.poll_company(res, cid, trigger=RunTrigger.BOARD)
    assert outcome.status == "failed"
    assert queue.jobs == []  # lỗi lặp lại không thành vòng poll liên tục
    assert await crawl_service.take_board_pending(res, cid)  # lượt poll theo lịch sau đọc lại


async def test_llm_fills_merged_jobs_after_rule_values_of_every_posting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tin careers (bản chuẩn) không rõ cấp bậc, tin TopCV cùng việc ghi "Senior": LLM chỉ được
    hỏi trường việc đã gộp còn thiếu, không ghi đè giá trị rule của tin khác trong cụm."""
    on_site = raw_job(external_id="c-1")
    on_board = raw_job(
        source="topcv",
        external_id="t-1",
        url="https://www.topcv.vn/viec-lam/ke-toan/t-1.html",
        level="Senior",
        employment_type="Toàn thời gian",
    )
    asked: list[tuple[JobLevel, list[str]]] = []
    saved: dict[str, Any] = {}

    async def context(*_args: Any) -> object:
        return object()

    async def accept_all(jobs: list[NormalizedJob], *_args: Any, **_kwargs: Any) -> list[Verdict]:
        return [Verdict(accepted=True, confidence=0.9, reasons=["TEST"]) for _ in jobs]

    async def classify(jobs: list[NormalizedJob], *_args: Any, **_kwargs: Any) -> int:
        for job in jobs:
            asked.append((job.level, job.unclear_fields))
            job.level = JobLevel.JUNIOR if job.level is JobLevel.UNKNOWN else job.level
            job.work_mode = WorkMode.ONSITE
        return len(jobs)

    async def save(_session: Any, **kwargs: Any) -> store.SaveResult:
        saved.update(kwargs)
        return store.SaveResult(jobs=[], postings=2, removed=0)

    monkeypatch.setattr(company_service, "company_context", context)
    monkeypatch.setattr(verify, "verify_jobs", accept_all)
    monkeypatch.setattr(normalize, "classify_unclear", classify)
    monkeypatch.setattr(store, "save_company_jobs", save)
    company: Any = SimpleNamespace(
        id=uuid4(), legal_name="ACME", brand_name=None, input_name="ACME"
    )
    gone = {("careers", "c-0")}
    await company_service.process_jobs(
        resources(),
        company,
        [on_site, on_board],
        website=None,
        deadline=0.0,
        complete=False,
        absent=gone,
    )
    assert asked == [(JobLevel.SENIOR, ["work_mode"])]
    [cluster] = saved["clusters"]
    [merged] = saved["merged"]
    assert (cluster.canonical.source, cluster.canonical.level) == ("careers", JobLevel.UNKNOWN)
    assert cluster.canonical.work_mode is WorkMode.UNKNOWN  # tin giữ giá trị rule
    assert (merged.level, merged.employment_type) == (JobLevel.SENIOR, EmploymentType.FULL_TIME)
    assert merged.work_mode is WorkMode.ONSITE
    assert saved["is_board"]("topcv")
    assert not saved["is_board"]("careers")
    assert saved["absent"] == gone
