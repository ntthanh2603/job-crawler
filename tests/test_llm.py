from __future__ import annotations

import asyncio
import json

import httpx2
import openai
import pytest
from pydantic import BaseModel

from jobcrawler.fakes import FakeChatClient
from jobcrawler.llm import (
    MAX_RATE_LIMIT_RETRIES,
    TRUNCATION_MARK,
    LLMClient,
    extract_json,
    is_rate_cap,
    retry_after,
    truncate,
)
from jobcrawler.net.ratelimit import MemoryRateLimiter
from jobcrawler.prompts import Prompt
from jobcrawler.prompts.healthcheck import PROMPT as HEALTHCHECK

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
