"""Client LLM (vLLM + Qwen qua API tương thích OpenAI) và hàm gọi structured output.

Mọi lời gọi trả JSON theo một schema Pydantic (`response_format=json_schema`), được kiểm tra
lại bằng `model_validate_json`. JSON lỗi thì gửi lại một lần kèm lỗi để model sửa; vẫn lỗi
thì kết quả là `unknown` (`value=None`). Không ném lỗi ra ngoài: pipeline coi `unknown` là
"không chắc" và tự quyết định bằng rule.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from jobcrawler.logging import get_logger
from jobcrawler.prompts.base import Prompt

if TYPE_CHECKING:
    from jobcrawler.config import Settings

log = get_logger(__name__)

TRUNCATION_MARK = "\n…[đã cắt bớt]"
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


@dataclass(slots=True)
class LLMResult[T: BaseModel]:
    value: T | None
    prompt: str
    version: str
    model: str | None
    duration_ms: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    attempts: int = 0
    error: str | None = None

    @property
    def unknown(self) -> bool:
        return self.value is None


def truncate(text: str, limit: int) -> str:
    """Cắt còn tối đa `limit` ký tự, ưu tiên cắt ở ranh giới đoạn/dòng/câu."""
    if len(text) <= limit:
        return text
    budget = max(0, limit - len(TRUNCATION_MARK))
    cut = text[:budget]
    for sep in ("\n\n", "\n", ". "):
        i = cut.rfind(sep)
        if i >= budget * 0.8:
            cut = cut[:i]
            break
    return cut.rstrip() + TRUNCATION_MARK


def extract_json(content: str) -> str:
    """Bỏ khối <think> và code fence nếu model lỡ trả kèm."""
    content = _THINK.sub("", content).strip()
    content = _FENCE.sub("", content).strip()
    if not content.startswith(("{", "[")):
        start, end = content.find("{"), content.rfind("}")
        if 0 <= start < end:
            content = content[start : end + 1]
    return content


class LLMClient:
    def __init__(
        self,
        client: Any | None,
        *,
        model: str | None,
        disable_thinking: bool = True,
        max_concurrency: int = 8,
        max_input_chars: int = 20_000,
        timeout: float = 30.0,
    ) -> None:
        self._client = client
        self.model = model
        self._disable_thinking = disable_thinking
        self._sem = asyncio.Semaphore(max_concurrency)
        self.max_input_chars = max_input_chars
        self._timeout = timeout

    @classmethod
    def from_settings(cls, settings: Settings) -> LLMClient:
        client = None
        if settings.llm_enabled:
            from openai import AsyncOpenAI

            # Retry do LLMClient tự làm (một lần) để giữ đúng giới hạn 30 giây mỗi lời gọi.
            client = AsyncOpenAI(
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key.get_secret_value(),
                timeout=settings.llm_timeout_seconds,
                max_retries=0,
            )
        return cls(
            client,
            model=settings.llm_model,
            disable_thinking=settings.llm_disable_thinking,
            max_concurrency=settings.llm_max_concurrency,
            max_input_chars=settings.llm_max_input_chars,
            timeout=settings.llm_timeout_seconds,
        )

    @property
    def enabled(self) -> bool:
        return self._client is not None and bool(self.model)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()

    def fit(self, prompt: Prompt, values: dict[str, str]) -> dict[str, str]:
        """Cắt các biến dài nhất để prompt người dùng không vượt `max_input_chars`."""
        values = dict(values)
        budget = self.max_input_chars - prompt.template_chars()
        while sum(len(v) for v in values.values()) > budget:
            excess = sum(len(v) for v in values.values()) - budget
            longest = max(values, key=lambda k: len(values[k]))
            new_len = max(0, len(values[longest]) - excess)
            shortened = truncate(values[longest], new_len) if new_len else ""
            if len(shortened) >= len(values[longest]):
                break
            values[longest] = shortened
        return values

    async def structured[T: BaseModel](
        self, prompt: Prompt, schema: type[T], **values: str
    ) -> LLMResult[T]:
        result: LLMResult[T] = LLMResult(
            value=None, prompt=prompt.name, version=prompt.version, model=self.model
        )
        if not self.enabled or self._client is None or self.model is None:
            result.error = "llm_disabled"
            return result

        messages: list[dict[str, str]] = prompt.render(self.fit(prompt, values))
        kwargs: dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema()},
            },
        }
        if self._disable_thinking:
            kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}

        import openai

        started = time.monotonic()
        async with self._sem:
            for attempt in (1, 2):
                result.attempts = attempt
                try:
                    async with asyncio.timeout(self._timeout):
                        resp = await self._client.chat.completions.create(
                            messages=messages, **kwargs
                        )
                except (TimeoutError, openai.APITimeoutError):
                    result.error = "timeout"
                    continue
                except openai.APIConnectionError as exc:
                    result.error = f"connection: {exc}"
                    continue
                except openai.APIStatusError as exc:
                    result.error = f"http_{exc.status_code}: {exc.message}"
                    if exc.status_code >= 500 or exc.status_code == 429:
                        continue
                    break

                usage = getattr(resp, "usage", None)
                if usage is not None:
                    result.prompt_tokens = (result.prompt_tokens or 0) + usage.prompt_tokens
                    result.completion_tokens = (
                        result.completion_tokens or 0
                    ) + usage.completion_tokens
                content = resp.choices[0].message.content or ""
                try:
                    result.value = schema.model_validate_json(extract_json(content))
                    result.error = None
                    break
                except ValidationError as exc:
                    result.error = f"parse_error: {exc.error_count()} lỗi"
                    messages = [
                        *messages,
                        {"role": "assistant", "content": content},
                        {
                            "role": "user",
                            "content": (
                                "JSON trên không hợp lệ với schema: "
                                f"{exc.errors(include_url=False)[:5]}. "
                                "Trả lại JSON đúng schema, không kèm giải thích."
                            ),
                        },
                    ]

        result.duration_ms = int((time.monotonic() - started) * 1000)
        log.info(
            "llm.call",
            prompt=prompt.name,
            version=prompt.version,
            model=self.model,
            ok=not result.unknown,
            duration_ms=result.duration_ms,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            attempts=result.attempts,
            error=result.error,
        )
        return result
