from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx2
import openai
from pydantic import BaseModel

from jobcrawler.common.config import Settings
from jobcrawler.prompts import Prompt
from jobcrawler.services import llm_metrics
from jobcrawler.services.fakes import FakeChatClient
from jobcrawler.services.llm import LLMClient, LLMResult, cached_structured
from jobcrawler.services.llm_metrics import LLMMetrics, error_kind
from jobcrawler.services.net.kv import MemoryKV

from .conftest import FakeClock

PREFIX = "jc-test"
NOW = 1_800_000_000.0  # giữa một giờ: 2027-01-15 08:00 UTC + 20 phút
HOUR = int(NOW // 3600)

PROMPT = Prompt(name="classify", version="1", system="Phân loại.", user='Văn bản: "$document"')


class Answer(BaseModel):
    is_job: bool


class FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.ops: list[tuple[str, tuple[Any, ...]]] = []

    async def __aenter__(self) -> FakePipeline:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def hincrby(self, key: str, name: str, amount: int) -> None:
        self.ops.append(("hincrby", (key, name, amount)))

    def expire(self, key: str, seconds: int) -> None:
        self.ops.append(("expire", (key, seconds)))

    def hgetall(self, key: str) -> None:
        self.ops.append(("hgetall", (key,)))

    def pttl(self, key: str) -> None:
        self.ops.append(("pttl", (key,)))

    async def execute(self) -> list[Any]:
        out: list[Any] = []
        for op, args in self.ops:
            if op == "hincrby":
                key, name, amount = args
                row = self.redis.hashes.setdefault(key, {})
                row[name.encode()] = row.get(name.encode(), 0) + amount
                out.append(row[name.encode()])
            elif op == "expire":
                self.redis.ttls[args[0]] = args[1]
                out.append(True)
            elif op == "hgetall":
                out.append(
                    {k: str(v).encode() for k, v in self.redis.hashes.get(args[0], {}).items()}
                )
            else:
                out.append(self.redis.pttls.get(args[0], -2))
        return out


class FakeRedis:
    def __init__(self) -> None:
        self.hashes: dict[str, dict[bytes, int]] = {}
        self.ttls: dict[str, int] = {}
        self.pttls: dict[str, int] = {}

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        return FakePipeline(self)


class ExplodingRedis:
    def pipeline(self, transaction: bool = True) -> Any:
        raise ConnectionError("redis down")


def settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "redis_prefix": PREFIX,
        "llm_base_url": "http://llm/v1",
        "llm_model": "qwen-test",
        "llm_requests_per_minute": 120,
    }
    return Settings(**(base | overrides))


def result(**fields: Any) -> LLMResult[Any]:
    base: dict[str, Any] = {
        "value": Answer(is_job=True),
        "prompt": "job_listing",
        "version": "1",
        "model": "qwen-test",
    }
    return LLMResult(**(base | fields))


def test_error_kind() -> None:
    assert error_kind("http_429: Too many requests") == "http_429"
    assert error_kind("rate_limited: chờ quá 30 s (http_429: x)") == "rate_limited"
    assert error_kind("timeout") == "timeout"
    assert error_kind("parse_error: 2 lỗi") == "parse_error"


async def test_record_then_collect_window_and_chart() -> None:
    redis = FakeRedis()
    metrics = LLMMetrics(redis, prefix=PREFIX)  # pyright: ignore[reportArgumentType]
    ok = result(duration_ms=1500, attempts=1, prompt_tokens=100, completion_tokens=20)
    await metrics.record(ok, now=NOW)
    await metrics.record(result(cached=True), now=NOW)
    failed = result(
        value=None,
        prompt="job_fields",
        duration_ms=31_000,
        attempts=2,
        throttled=3,
        error="rate_limited: chờ quá 30 s (http_429: rpm)",
    )
    await metrics.record(failed, now=NOW - 1800)  # giờ trước
    await metrics.record(ok, now=NOW - 3 * 3600)  # ngoài cửa sổ 1 giờ, vẫn trong biểu đồ 24 giờ
    redis.pttls[f"{PREFIX}:rl:pause:llm"] = 12_340
    assert redis.ttls[llm_metrics.hour_key(PREFIX, HOUR)] == llm_metrics.TTL

    st = await llm_metrics.collect(redis, settings(), hours=1, now=NOW)  # pyright: ignore[reportArgumentType]
    assert st.since == datetime.fromtimestamp((HOUR - 1) * 3600, UTC)
    assert (st.enabled, st.model, st.requests_per_minute) == (True, "qwen-test", 120)
    assert st.paused_until == datetime.fromtimestamp(NOW + 12.34, UTC)
    assert (st.calls, st.ok, st.cached, st.failed) == (3, 2, 1, 1)
    assert (st.throttled, st.attempts) == (3, 3)
    assert (st.prompt_tokens, st.completion_tokens) == (100, 20)
    assert st.avg_ms == (1500 + 31_000) // 2
    assert (st.p50_seconds, st.p95_seconds) == (2.0, 45.0)
    assert st.errors == [("rate_limited", 1)]
    assert [(p.prompt, p.calls, p.ok, p.cached, p.avg_ms) for p in st.prompts] == [
        ("job_listing", 2, 2, 1, 1500),
        ("job_fields", 1, 0, 0, 31_000),
    ]
    assert len(st.chart) == llm_metrics.CHART_HOURS
    assert st.chart[-1].hour == datetime.fromtimestamp(HOUR * 3600, UTC)
    assert (st.chart[-1].ok, st.chart[-1].cached, st.chart[-1].failed) == (1, 1, 0)
    assert (st.chart[-1].prompt_tokens, st.chart[-1].completion_tokens) == (100, 20)
    assert st.chart[-2].failed == 1
    assert st.chart[-4].ok == 1

    everything = await llm_metrics.collect(redis, settings(), hours=None, now=NOW)  # pyright: ignore[reportArgumentType]
    assert everything.calls == 4
    assert everything.since == datetime.fromtimestamp(
        (HOUR - llm_metrics.RETENTION_HOURS) * 3600, UTC
    )


async def test_collect_empty_and_disabled() -> None:
    st = await llm_metrics.collect(
        FakeRedis(),  # pyright: ignore[reportArgumentType]
        settings(llm_base_url=None),
        hours=24,
        now=NOW,
    )
    assert not st.enabled
    assert st.paused_until is None
    assert (st.calls, st.avg_ms, st.p95_seconds, st.per_hour) == (0, None, None, None)
    assert st.prompts == []
    assert st.errors == []
    assert [h.ok + h.cached + h.failed for h in st.chart] == [0] * 24


async def test_record_swallows_redis_errors() -> None:
    metrics = LLMMetrics(ExplodingRedis(), prefix=PREFIX)  # pyright: ignore[reportArgumentType]
    await metrics.record(result(), now=NOW)  # không ném lỗi


def rate_limited() -> openai.RateLimitError:
    request = httpx2.Request("POST", "http://llm/v1/chat/completions")
    return openai.RateLimitError(
        "Error code: 429",
        response=httpx2.Response(429, request=request),
        body={"detail": {"error": "key_concurrency_limit", "retry_after": 1}},
    )


async def test_llm_client_records_calls_cache_hits_and_throttling(clock: FakeClock) -> None:
    redis = FakeRedis()
    metrics = LLMMetrics(redis, prefix=PREFIX)  # pyright: ignore[reportArgumentType]
    fake = FakeChatClient([rate_limited(), '{"is_job": true}'])
    llm = LLMClient(fake, model="qwen-test", metrics=metrics, sleep=clock.sleep)  # pyright: ignore[reportArgumentType]
    kv = MemoryKV()

    first = await cached_structured(llm, kv, "k", PROMPT, Answer, ttl=60, document="x")
    again = await cached_structured(llm, kv, "k", PROMPT, Answer, ttl=60, document="x")
    assert (first.throttled, first.cached, again.cached) == (1, False, True)

    st = await llm_metrics.collect(redis, settings(), hours=1)  # pyright: ignore[reportArgumentType]
    assert (st.calls, st.ok, st.cached, st.throttled, st.attempts) == (2, 2, 1, 1, 2)
    assert [(p.prompt, p.calls, p.cached) for p in st.prompts] == [("classify", 2, 1)]


async def test_disabled_llm_is_not_recorded() -> None:
    redis = FakeRedis()
    llm = LLMClient(None, model=None, metrics=LLMMetrics(redis, prefix=PREFIX))  # pyright: ignore[reportArgumentType]
    out = await llm.structured(PROMPT, Answer, document="x")
    assert out.error == "llm_disabled"
    assert redis.hashes == {}
