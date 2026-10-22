from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx2
import openai
import pytest
from pydantic import BaseModel

from jobcrawler.prompts import Prompt
from jobcrawler.prompts.base import PRIORITY_COLLECT, PRIORITY_ENRICH, PRIORITY_VERIFY
from jobcrawler.prompts.healthcheck import PROMPT as HEALTHCHECK
from jobcrawler.services.fakes import FakeChatClient
from jobcrawler.services.llm import (
    MAX_RATE_LIMIT_RETRIES,
    TRUNCATION_MARK,
    LLMClient,
    PrioritySemaphore,
    cached_structured,
    extract_json,
    is_rate_cap,
    retry_after,
    truncate,
)
from jobcrawler.services.net.kv import MemoryKV
from jobcrawler.services.net.ratelimit import MemoryRateLimiter

from .conftest import FakeClock

PROMPT = Prompt(
    name="classify",
    version="1",
    system="Phân loại.",
    user='Văn bản: "$document"\nVí dụ JSON: {"is_job": true}',
)


class Answer(BaseModel):
    is_job: bool
    confidence: float


def make(replies: list[str | BaseException], **kwargs: object) -> tuple[LLMClient, FakeChatClient]:
    fake = FakeChatClient(replies, delay=float(kwargs.pop("delay", 0.0)))  # pyright: ignore[reportArgumentType]
    return LLMClient(fake, model="qwen-test", **kwargs), fake  # pyright: ignore[reportArgumentType]


async def test_valid_json_request_shape() -> None:
    llm, fake = make(['{"is_job": true, "confidence": 0.9}'])
    result = await llm.structured(PROMPT, Answer, document="Tuyển dev")
    assert result.value == Answer(is_job=True, confidence=0.9)
    assert (result.attempts, result.error, result.prompt, result.version) == (
        1,
        None,
        "classify",
        "1",
    )
    assert result.prompt_tokens == 10

    [call] = fake.calls
    assert call["model"] == "qwen-test"
    assert call["temperature"] == 0
    fmt = call["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["name"] == "Answer"
    assert fmt["json_schema"]["schema"] == Answer.model_json_schema()
    assert call["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    system, user = call["messages"]
    assert system == {"role": "system", "content": "Phân loại."}
    assert user["content"] == 'Văn bản: "Tuyển dev"\nVí dụ JSON: {"is_job": true}'


async def test_thinking_can_stay_enabled() -> None:
    llm, fake = make(['{"is_job": true, "confidence": 1}'], disable_thinking=False)
    await llm.structured(PROMPT, Answer, document="x")
    assert "extra_body" not in fake.calls[0]


async def test_invalid_json_is_retried_once_with_feedback() -> None:
    llm, fake = make(['{"is_job": "có lẽ"}', '{"is_job": false, "confidence": 0.2}'])
    result = await llm.structured(PROMPT, Answer, document="x")
    assert result.value == Answer(is_job=False, confidence=0.2)
    assert result.attempts == 2
    retry_messages = fake.calls[1]["messages"]
    assert retry_messages[2] == {"role": "assistant", "content": '{"is_job": "có lẽ"}'}
    assert "không hợp lệ" in retry_messages[3]["content"]


async def test_invalid_twice_is_unknown() -> None:
    llm, fake = make(["không phải JSON"])
    result = await llm.structured(PROMPT, Answer, document="x")
    assert result.unknown
    assert result.attempts == 2
    assert result.error is not None
    assert result.error.startswith("parse_error")
    assert len(fake.calls) == 2


async def test_transient_errors_retry_then_unknown() -> None:
    request = httpx2.Request("POST", "http://llm/v1/chat/completions")
    llm, fake = make(
        [openai.APIConnectionError(request=request), '{"is_job": true, "confidence": 1}']
    )
    assert (await llm.structured(PROMPT, Answer, document="x")).value is not None

    llm, fake = make([TimeoutError()])
    result = await llm.structured(PROMPT, Answer, document="x")
    assert (result.unknown, result.error, len(fake.calls)) == (True, "timeout", 2)


async def test_client_errors_are_not_retried() -> None:
    request = httpx2.Request("POST", "http://llm/v1/chat/completions")
    error = openai.BadRequestError(
        "bad schema", response=httpx2.Response(400, request=request), body=None
    )
    llm, fake = make([error])
    result = await llm.structured(PROMPT, Answer, document="x")
    assert result.unknown
    assert result.error is not None
    assert result.error.startswith("http_400")
    assert len(fake.calls) == 1


class _OddClient:
    """Phản hồi sai dạng mà endpoint / SDK vẫn có thể trả: `choices` rỗng, `usage` thiếu số."""

    def __init__(self, replies: list[object]) -> None:
        self._replies = replies
        self.chat = self
        self.completions = self

    async def create(self, **kwargs: object) -> object:
        reply = self._replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


async def test_malformed_responses_are_errors_not_exceptions() -> None:
    """Không ném lỗi ra ngoài (trong `gather_all` một lỗi huỷ cả các lời gọi khác)."""
    content = '{"is_job": true, "confidence": 1}'
    ok = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=None, completion_tokens=None),
    )
    empty = SimpleNamespace(choices=[], usage=None)
    llm = LLMClient(_OddClient([empty, ok]), model="qwen-test")
    result = await llm.structured(PROMPT, Answer, document="x")
    assert (result.value, result.attempts, result.prompt_tokens) == (
        Answer(is_job=True, confidence=1),
        2,
        0,
    )

    request = httpx2.Request("POST", "http://llm/v1/chat/completions")
    invalid = openai.APIResponseValidationError(
        response=httpx2.Response(200, request=request), body=None
    )
    llm = LLMClient(_OddClient([empty, invalid]), model="qwen-test")
    result = await llm.structured(PROMPT, Answer, document="x")
    assert result.unknown
    assert result.attempts == 2
    assert (result.error or "").startswith("error: APIResponseValidationError")


async def test_call_timeout_is_enforced() -> None:
    llm, _ = make(['{"is_job": true, "confidence": 1}'], delay=0.2, timeout=0.05)
    result = await llm.structured(PROMPT, Answer, document="x")
    assert (result.unknown, result.error) == (True, "timeout")


async def test_disabled_client_returns_unknown_without_calling() -> None:
    llm = LLMClient(None, model=None)
    result = await llm.structured(PROMPT, Answer, document="x")
    assert (llm.enabled, result.unknown, result.error) == (False, True, "llm_disabled")


async def test_long_input_is_truncated() -> None:
    llm, fake = make(['{"is_job": true, "confidence": 1}'], max_input_chars=1000)
    document = ("Mô tả công việc rất dài.\n\n" * 200).strip()
    await llm.structured(PROMPT, Answer, document=document)
    user = fake.calls[0]["messages"][1]["content"]
    assert len(user) <= 1000
    assert TRUNCATION_MARK in user
    assert user.endswith('Ví dụ JSON: {"is_job": true}')  # phần cố định của prompt còn nguyên


async def test_concurrency_is_limited() -> None:
    llm, fake = make(['{"is_job": true, "confidence": 1}'], delay=0.02, max_concurrency=2)
    await asyncio.gather(*(llm.structured(PROMPT, Answer, document="x") for _ in range(6)))
    assert fake.max_active == 2


def test_truncate_prefers_boundaries() -> None:
    text = "Đoạn một.\n\n" + "x" * 100
    assert truncate(text, 1000) == text
    cut = truncate("a" * 50 + "\n\n" + "b" * 50, 70)
    assert cut == "a" * 50 + TRUNCATION_MARK
    assert len(truncate("y" * 500, 100)) == 100


@pytest.mark.parametrize(
    "content",
    [
        '{"a": 1}',
        '<think>suy nghĩ...</think>\n{"a": 1}',
        '```json\n{"a": 1}\n```',
        'Kết quả: {"a": 1} xong',
    ],
)
def test_extract_json(content: str) -> None:
    assert json.loads(extract_json(content)) == {"a": 1}


def test_healthcheck_prompt_renders() -> None:
    messages = HEALTHCHECK.render({"nonce": "abc123", "sentence": "Xin chào"})
    assert "`abc123`" in messages[1]["content"]
    assert '"Xin chào"' in messages[1]["content"]


def rate_limited(error: str, after: float) -> openai.RateLimitError:
    request = httpx2.Request("POST", "http://llm/v1/chat/completions")
    return openai.RateLimitError(
        "Error code: 429",
        response=httpx2.Response(429, request=request),
        body={"detail": {"error": error, "retry_after": after}},
    )


OK = '{"is_job": true, "confidence": 1}'


async def test_concurrency_429_waits_retry_after_and_retries(clock: FakeClock) -> None:
    llm, fake = make([rate_limited("key_concurrency_limit", 1), OK], sleep=clock.sleep)
    result = await llm.structured(PROMPT, Answer, document="x")
    assert result.value is not None
    assert result.attempts == 2
    assert len(fake.calls) == 2
    assert len(clock.sleeps) == 1
    assert 1 <= clock.sleeps[0] <= 1.5


async def test_rpm_429_pauses_shared_bucket(clock: FakeClock) -> None:
    limiter = MemoryRateLimiter(clock=clock, sleep=clock.sleep)
    llm, fake = make(
        [rate_limited("rpm_limit_exceeded", 20), OK],
        limiter=limiter,
        requests_per_minute=60,
    )
    result = await llm.structured(PROMPT, Answer, document="x")
    assert result.value is not None
    assert len(fake.calls) == 2
    assert limiter.calls == [("llm", 1.0, 1), ("llm", 1.0, 1)]  # 60/phút = 1 request/giây
    assert sum(clock.sleeps) >= 20  # lượt sau chờ hết lần tạm dừng chung


async def test_rate_limit_gives_up_when_wait_exceeds_timeout(clock: FakeClock) -> None:
    limiter = MemoryRateLimiter(clock=clock, sleep=clock.sleep)
    llm, fake = make([rate_limited("rpm_limit_exceeded", 60)], limiter=limiter, timeout=30)
    result = await llm.structured(PROMPT, Answer, document="x")
    assert result.unknown
    assert result.error is not None
    assert result.error.startswith("http_429")
    assert len(fake.calls) == 1

    # Bị 429 liên tục: dừng sau MAX_RATE_LIMIT_RETRIES lần thử lại.
    llm, fake = make([rate_limited("global_concurrency_limit", 0)], sleep=clock.sleep)
    result = await llm.structured(PROMPT, Answer, document="x")
    assert result.unknown
    assert len(fake.calls) == MAX_RATE_LIMIT_RETRIES + 1


def test_retry_after_parsing() -> None:
    assert retry_after(rate_limited("rpm_limit_exceeded", 60)) == 60
    assert is_rate_cap(rate_limited("rpm_limit_exceeded", 60))
    assert not is_rate_cap(rate_limited("key_concurrency_limit", 1))
    assert not is_rate_cap(rate_limited("global_concurrency_limit", 1))
    assert retry_after(ValueError()) == 1.0


async def test_cache_is_per_model() -> None:
    """Đổi LLM_MODEL thì hỏi lại: câu trả lời của model cũ không được dùng lại."""
    kv = MemoryKV()
    reply = '{"is_job": true, "confidence": 0.9}'
    old, _ = make([reply])
    first = await cached_structured(old, kv, "h1", PROMPT, Answer, ttl=60, document="x")
    again = await cached_structured(old, kv, "h1", PROMPT, Answer, ttl=60, document="x")
    assert first.value is not None
    assert not first.cached
    assert again.cached
    new = LLMClient(FakeChatClient([reply]), model="qwen-new")  # pyright: ignore[reportArgumentType]
    other = await cached_structured(new, kv, "h1", PROMPT, Answer, ttl=60, document="x")
    assert other.value is not None
    assert not other.cached


async def test_priority_semaphore_serves_lower_priority_number_first() -> None:
    sem = PrioritySemaphore(1)
    order: list[str] = []
    await sem.acquire(0)

    async def call(name: str, priority: int) -> None:
        async with sem.hold(priority):
            order.append(name)

    tasks = [
        asyncio.create_task(call("enrich", PRIORITY_ENRICH)),
        asyncio.create_task(call("verify", PRIORITY_VERIFY)),
        asyncio.create_task(call("collect-1", PRIORITY_COLLECT)),
        asyncio.create_task(call("collect-2", PRIORITY_COLLECT)),
    ]
    await asyncio.sleep(0)
    sem.release()
    await asyncio.gather(*tasks)
    assert order == ["collect-1", "collect-2", "verify", "enrich"]


async def test_priority_semaphore_skips_cancelled_waiters() -> None:
    sem = PrioritySemaphore(1)
    await sem.acquire(0)
    waiting = asyncio.create_task(sem.acquire(0))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    sem.release()
    # Chỗ trống không bị lời gọi đã huỷ giữ: lời gọi sau vào ngay.
    await asyncio.wait_for(sem.acquire(PRIORITY_ENRICH), timeout=1)


async def test_slot_granted_to_cancelled_waiter_passes_on() -> None:
    sem = PrioritySemaphore(1)
    await sem.acquire(0)
    first = asyncio.create_task(sem.acquire(0))
    second = asyncio.create_task(sem.acquire(PRIORITY_ENRICH))
    await asyncio.sleep(0)
    sem.release()  # trao lượt cho `first`...
    first.cancel()  # ...nhưng nó bị huỷ trước khi chạy tiếp
    with pytest.raises(asyncio.CancelledError):
        await first
    await asyncio.wait_for(second, timeout=1)


async def test_concurrency_429_shrinks_then_success_grows_back(clock: FakeClock) -> None:
    """Khoá bị giới hạn số lời gọi đồng thời: process bớt chỗ, chạy trơn thì thêm lại dần."""
    replies: list[str | BaseException] = [rate_limited("key_concurrency_limit", 1)] * 3
    llm, _ = make([*replies, *[OK] * 40], sleep=clock.sleep, max_concurrency=4)
    sem = llm._sem  # pyright: ignore[reportPrivateUsage]
    await llm.structured(PROMPT, Answer, document="x")
    assert sem.capacity == 1
    for _ in range(4 * 1 + 4 * 2 + 4 * 3):
        await llm.structured(PROMPT, Answer, document="x")
    assert sem.capacity == 4


class FakeSlots:
    """Hàng chung giả của cả cụm (như `RedisSemaphore`) trong một process."""

    def __init__(self, limit: int, *, grant: bool = True) -> None:
        self.sem = PrioritySemaphore(limit)
        self.grant = grant
        self.held: set[str] = set()
        self.priorities: list[int] = []
        self.shrinks = 0
        self.grows = 0
        self._ids = iter(range(10**6))

    async def acquire(self, priority: int, *, timeout: float) -> str | None:
        self.priorities.append(priority)
        if not self.grant:
            return None
        await self.sem.acquire(priority)
        token = f"t{next(self._ids)}"
        self.held.add(token)
        return token

    async def release(self, token: str) -> None:
        self.held.discard(token)
        self.sem.release()

    async def shrink(self) -> int:
        self.shrinks += 1
        return 1

    async def grow(self) -> int:
        self.grows += 1
        return 1


async def test_cluster_slots_limit_calls_instead_of_process_limit() -> None:
    slots = FakeSlots(2)
    llm, fake = make([OK], delay=0.02, max_concurrency=8, slots=slots)
    await asyncio.gather(*(llm.structured(PROMPT, Answer, document="x") for _ in range(6)))
    assert fake.max_active == 2
    assert slots.held == set()  # mọi chỗ đã trả
    assert slots.grows == 6


class CountingLimiter:
    """Token bucket giả: đếm số lời gọi đang cùng chờ token."""

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

    async def acquire(self, domain: str, *, rate: float, burst: int = 1) -> float:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        return 0.01

    async def penalize(self, domain: str, seconds: float) -> None:
        return None


async def test_only_slot_holders_wait_for_the_rate_limit_token() -> None:
    """Hàng chung trước, token request/phút sau: hàng trăm lời gọi không cùng lao vào bucket rồi
    hết `timeout` (lời gọi chờ lâu trong hàng chung theo ưu tiên thay vào đó)."""
    slots = FakeSlots(2)
    limiter = CountingLimiter()
    llm, fake = make([OK], slots=slots, limiter=limiter, requests_per_minute=600)
    results = await asyncio.gather(
        *(llm.structured(PROMPT, Answer, document="x") for _ in range(8))
    )
    assert all(r.value is not None for r in results)
    assert limiter.max_active <= 2
    assert fake.max_active <= 2


async def test_cluster_slot_wait_too_long_gives_up_without_calling() -> None:
    slots = FakeSlots(1, grant=False)
    llm, fake = make([OK], slots=slots, queue_wait=5)
    result = await llm.structured(PROMPT, Answer, document="x")
    assert result.value is None
    assert result.error == "rate_limited: chờ lượt LLM chung quá 5 s"
    assert result.attempts == 0
    assert fake.calls == []


async def test_cluster_slot_is_released_while_backing_off_after_429() -> None:
    slots = FakeSlots(1)
    held_while_sleeping: list[int] = []

    async def sleep(seconds: float) -> None:
        held_while_sleeping.append(len(slots.held))

    llm, fake = make(
        [rate_limited("key_concurrency_limit", 1), OK], sleep=sleep, max_concurrency=4, slots=slots
    )
    result = await llm.structured(PROMPT, Answer, document="x")
    assert result.value is not None
    assert len(fake.calls) == 2
    assert held_while_sleeping == [0]  # chờ sau 429 không giữ chỗ chung
    assert slots.shrinks == 1  # cả cụm bớt chỗ, không phải process
    assert llm._sem.capacity == 4  # pyright: ignore[reportPrivateUsage]
    assert slots.priorities == [PROMPT.priority, PROMPT.priority]
