from __future__ import annotations

import asyncio
import json

import httpx2
import openai
import pytest
from pydantic import BaseModel

from jobcrawler.fakes import FakeChatClient
from jobcrawler.llm import TRUNCATION_MARK, LLMClient, extract_json, truncate
from jobcrawler.prompts import Prompt
from jobcrawler.prompts.healthcheck import PROMPT as HEALTHCHECK

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
