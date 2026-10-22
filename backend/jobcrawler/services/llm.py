"""Client LLM (vLLM + Qwen qua API tương thích OpenAI) và hàm gọi structured output.

Mọi lời gọi trả JSON theo một schema Pydantic (`response_format=json_schema`), được kiểm tra
lại bằng `model_validate_json`. JSON lỗi thì gửi lại một lần kèm lỗi để model sửa; vẫn lỗi
thì kết quả là `unknown` (`value=None`). Không ném lỗi ra ngoài: pipeline coi `unknown` là
"không chắc" và tự quyết định bằng rule.

Endpoint dùng chung có giới hạn request/phút và số lời gọi đồng thời (trả 429 kèm
`retry_after`). Mọi worker lấy lượt từ một token bucket trên Redis (LLM_REQUESTS_PER_MINUTE);
429 vì vượt request/phút thì tạm dừng cả cụm trong `retry_after`, 429 vì đồng thời thì chờ
`retry_after` rồi thử lại. Thời gian chờ tối đa mỗi lời gọi bằng LLM_TIMEOUT_SECONDS; bước gọi
LLM nào cũng có deadline riêng và huỷ lời gọi đang chờ khi hết giờ.

Giới hạn số lời gọi đồng thời của endpoint tính theo khoá, chung mọi process: có `slots`
(`services/redis/semaphore.RedisSemaphore`, LLM_CLUSTER_CONCURRENCY) thì mỗi lời gọi lấy một chỗ
trong hàng chung của cả cụm trước (theo ưu tiên của prompt, chờ tối đa LLM_QUEUE_WAIT_SECONDS),
nên lời gọi vượt giới hạn xếp hàng thay vì bị 429; 429 vì đồng thời thì cả cụm bớt một chỗ, chạy
trơn thì thêm lại dần.
"""

from __future__ import annotations

import asyncio
import contextlib
import heapq
import itertools
import random
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from jobcrawler.common.logging import get_logger
from jobcrawler.prompts.base import Prompt

if TYPE_CHECKING:
    from jobcrawler.common.config import Settings
    from jobcrawler.services.llm_metrics import LLMMetrics
    from jobcrawler.services.net.kv import KeyValueStore
    from jobcrawler.services.net.ratelimit import RateLimiter
    from jobcrawler.services.redis.semaphore import Slots

log = get_logger(__name__)

TRUNCATION_MARK = "\n…[đã cắt bớt]"
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
# Số lần thử lại tối đa khi bị 429 (không tính vào hai lần thử thường).
MAX_RATE_LIMIT_RETRIES = 6
LIMITER_KEY = "llm"


def retry_after(exc: Any) -> float:
    """`retry_after` (giây) trong body 429 (`{"detail": {"retry_after": 60}}`) hoặc header
    Retry-After; mặc định 1."""
    body = getattr(exc, "body", None)
    detail = body.get("detail") if isinstance(body, dict) else None
    value: Any = detail.get("retry_after") if isinstance(detail, dict) else None
    if value is None:
        response = getattr(exc, "response", None)
        value = response.headers.get("retry-after") if response is not None else None
    try:
        return max(0.0, float(value)) if value is not None else 1.0
    except (TypeError, ValueError):
        return 1.0


def is_rate_cap(exc: Any) -> bool:
    """429 vì vượt request/phút (khác 429 vì quá nhiều lời gọi đồng thời)."""
    text = f"{getattr(exc, 'body', '')} {getattr(exc, 'message', '')}".lower()
    return "rpm" in text or "per minute" in text or ("rate" in text and "concurren" not in text)


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
    throttled: int = 0  # số phản hồi 429
    error: str | None = None
    cached: bool = False  # lấy từ cache (`cached_structured`), không gọi LLM

    @property
    def unknown(self) -> bool:
        return self.value is None


class PrioritySemaphore:
    """Semaphore xếp lời gọi đang chờ theo ưu tiên (`Prompt.priority`, số nhỏ trước; cùng mức thì
    ai chờ trước vào trước): khi LLM quá tải, lời gọi tìm nguồn / bóc tin của doanh nghiệp đang
    chạy không phải xếp sau hàng chục lời gọi phân loại thêm trường của doanh nghiệp khác.

    Số chỗ co giãn trong [1, `value`]: endpoint báo quá số lời gọi đồng thời của khoá (429
    `key_concurrency_limit`, giới hạn đổi theo nhà cung cấp và tính chung mọi process) thì bớt một
    chỗ (`shrink`); chạy trơn thì thêm lại dần (`grow`)."""

    def __init__(self, value: int) -> None:
        self.limit = value
        self.capacity = value
        self._value = value
        self._waiters: list[tuple[int, int, asyncio.Future[None]]] = []
        self._order = itertools.count()

    def shrink(self) -> None:
        if self.capacity > 1:
            self.capacity -= 1
            self._value -= 1  # có thể âm: chỗ đang dùng trả lại thì không trao tiếp

    def grow(self) -> None:
        if self.capacity < self.limit:
            self.capacity += 1
            self._value += 1
            self._wake()

    async def acquire(self, priority: int) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        heapq.heappush(self._waiters, (priority, next(self._order), future))
        self._wake()
        try:
            await future
        except asyncio.CancelledError:
            if not future.cancelled():
                self.release()  # được trao lượt đúng lúc bị huỷ: trao tiếp cho người sau
            raise

    def release(self) -> None:
        self._value += 1
        self._wake()

    def _wake(self) -> None:
        # Lời gọi đã huỷ (future xong) nằm lại trong heap tới khi tới lượt thì bỏ qua.
        while self._value > 0 and self._waiters:
            _, _, future = heapq.heappop(self._waiters)
            if not future.done():
                self._value -= 1
                future.set_result(None)

    @contextlib.asynccontextmanager
    async def hold(self, priority: int) -> AsyncIterator[None]:
        await self.acquire(priority)
        try:
            yield
        finally:
            self.release()


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
        limiter: RateLimiter | None = None,
        requests_per_minute: float | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        cache: KeyValueStore | None = None,
        metrics: LLMMetrics | None = None,
        slots: Slots | None = None,
        queue_wait: float = 300.0,
    ) -> None:
        self._client = client
        self.model = model
        self._disable_thinking = disable_thinking
        self._sem = PrioritySemaphore(max_concurrency)
        self._calm = 0  # lời gọi thành công liên tiếp từ lần bị 429 vì đồng thời
        self.max_input_chars = max_input_chars
        self._timeout = timeout
        self._limiter = limiter
        self._rpm = requests_per_minute
        self._sleep = sleep
        # Cache câu trả lời theo nội dung (`cached()`), dùng chung mọi worker (Redis).
        self.cache = cache
        # Số đo lời gọi cho trang Tổng quan (`llm_metrics`), cộng dồn trên Redis chính.
        self.metrics = metrics
        # Hàng chung của cả cụm (giới hạn số lời gọi đồng thời theo khoá của endpoint).
        self._slots = slots
        self._queue_wait = queue_wait

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        limiter: RateLimiter | None = None,
        cache: KeyValueStore | None = None,
        metrics: LLMMetrics | None = None,
        slots: Slots | None = None,
    ) -> LLMClient:
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
            limiter=limiter,
            requests_per_minute=settings.llm_requests_per_minute,
            cache=cache,
            metrics=metrics,
            slots=slots,
            queue_wait=settings.llm_queue_wait_seconds,
        )

    @property
    def enabled(self) -> bool:
        return self._client is not None and bool(self.model)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()

    async def _wait_turn(self, budget: float) -> bool:
        """Lấy lượt từ token bucket chung (và chờ hết lần tạm dừng do 429); False nếu phải chờ
        quá `budget` giây."""
        if self._limiter is None:
            return True
        # Không cấu hình request/phút: bucket rất rộng, chỉ để tôn trọng lần tạm dừng chung.
        rate, burst = (self._rpm / 60, 1) if self._rpm else (1000.0, 1000)
        if budget <= 0:
            return False
        try:
            async with asyncio.timeout(budget):
                await self._limiter.acquire(LIMITER_KEY, rate=rate, burst=burst)
        except TimeoutError:
            return False
        return True

    async def _back_off(self, exc: Any, budget: float) -> bool:
        """Sau 429: tạm dừng cả cụm (vượt request/phút) hoặc chờ `retry_after` (quá đồng thời).
        False nếu không còn đủ `budget` giây để thử lại."""
        delay = retry_after(exc)
        if is_rate_cap(exc) and self._limiter is not None:
            await self._limiter.penalize(LIMITER_KEY, delay)
            return delay < budget  # lần chờ thật nằm ở `_wait_turn`
        if not is_rate_cap(exc):
            # Quá số lời gọi đồng thời của khoá: cả cụm bớt chỗ (hàng chung), không có hàng chung
            # thì process này bớt một chỗ.
            if self._slots is not None:
                with contextlib.suppress(Exception):
                    await self._slots.shrink()
            else:
                self._sem.shrink()
                self._calm = 0
        delay += random.uniform(0, 0.5)
        if delay >= budget:
            return False
        await self._sleep(delay)
        return True

    async def cached[T: BaseModel](
        self, prompt: Prompt, schema: type[T], *, key: str, ttl: float, **values: str
    ) -> LLMResult[T]:
        """Như `structured` nhưng dùng lại câu trả lời cũ cho cùng prompt + `key` (hash nội
        dung) trong `ttl` giây, nếu client có cache."""
        return await cached_structured(self, self.cache, key, prompt, schema, ttl=ttl, **values)

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
        client = self._client
        # Có hàng chung của cả cụm thì nó thay chỗ của process (`_slot`).
        hold = self._sem.hold(prompt.priority) if self._slots is None else contextlib.nullcontext()
        queue_deadline = started + self._queue_wait
        waited = 0.0  # tổng thời gian chờ lượt / chờ sau 429, tối đa `timeout`
        tries = 0  # lần thử thường (lỗi tạm thời, JSON sai): tối đa 2
        limited = 0  # lần bị 429
        async with hold:
            while tries < 2:
                resp: Any = None
                failure: Exception | None = None
                # Chỗ chung giữ trong lúc chờ token request/phút và chờ trả lời (chỉ người đang giữ
                # chỗ tranh token); lỗi xử lý sau khi trả chỗ, chờ sau 429 không giữ chỗ.
                async with self._slot(prompt.priority, queue_deadline) as granted:
                    if not granted:
                        result.error = (
                            f"rate_limited: chờ lượt LLM chung quá {self._queue_wait:g} s"
                        )
                        break
                    mark = time.monotonic()
                    turn = await self._wait_turn(self._timeout - waited)
                    waited += time.monotonic() - mark
                    if not turn:
                        result.error = f"rate_limited: chờ quá {self._timeout:g} s" + (
                            f" ({result.error})" if result.error else ""
                        )
                        break
                    tries += 1
                    result.attempts += 1
                    try:
                        async with asyncio.timeout(self._timeout):
                            resp = await client.chat.completions.create(messages=messages, **kwargs)
                    except Exception as exc:
                        failure = exc
                if failure is not None:
                    try:
                        raise failure
                    except (TimeoutError, openai.APITimeoutError):
                        result.error = "timeout"
                        continue
                    except openai.APIConnectionError as exc:
                        result.error = f"connection: {exc}"
                        continue
                    except openai.APIStatusError as exc:
                        result.error = f"http_{exc.status_code}: {exc.message}"
                        if exc.status_code == 429:
                            tries -= 1  # không tính vào số lần thử thường
                            limited += 1
                            result.throttled = limited
                            mark = time.monotonic()
                            again = limited <= MAX_RATE_LIMIT_RETRIES and await self._back_off(
                                exc, self._timeout - waited
                            )
                            waited += time.monotonic() - mark
                            if again:
                                continue
                            break
                        if exc.status_code >= 500:
                            continue
                        break
                    except Exception as exc:  # phản hồi sai dạng (APIResponseValidationError...)
                        result.error = f"error: {type(exc).__name__}: {exc}"[:500]
                        continue

                usage = getattr(resp, "usage", None)
                if usage is not None:
                    result.prompt_tokens = (result.prompt_tokens or 0) + (
                        getattr(usage, "prompt_tokens", None) or 0
                    )
                    result.completion_tokens = (result.completion_tokens or 0) + (
                        getattr(usage, "completion_tokens", None) or 0
                    )
                choices = getattr(resp, "choices", None) or []
                message = getattr(choices[0], "message", None) if choices else None
                if message is None:
                    result.error = "empty_response"
                    continue
                content = getattr(message, "content", None) or ""
                try:
                    result.value = schema.model_validate_json(extract_json(content))
                    result.error = None
                    if self._slots is not None:  # chạy trơn: cả cụm thêm lại chỗ dần
                        with contextlib.suppress(Exception):
                            await self._slots.grow()
                    else:
                        self._calm += 1
                        if self._calm >= self._sem.capacity * 4:  # chạy trơn: thêm lại một chỗ
                            self._sem.grow()
                            self._calm = 0
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
        if self.metrics is not None:
            await self.metrics.record(result)
        return result

    @contextlib.asynccontextmanager
    async def _slot(self, priority: int, deadline: float) -> AsyncIterator[bool]:
        """Một chỗ trong hàng chung của cả cụm cho một lần gửi (không có hàng chung: luôn có),
        xếp hàng theo `priority` tới `deadline` (monotonic); False nếu chờ quá."""
        slots = self._slots
        if slots is None:
            yield True
            return
        token = await slots.acquire(priority, timeout=max(0.0, deadline - time.monotonic()))
        if token is None:
            yield False
            return
        try:
            yield True
        finally:
            with contextlib.suppress(Exception):
                await asyncio.shield(slots.release(token))


def cache_key(prompt: Prompt, key: str, model: str | None) -> str:
    """Khoá cache câu trả lời: theo prompt (cả version) và model — đổi LLM_MODEL thì hỏi lại, không
    dùng câu trả lời của model cũ."""
    return f"llm:{prompt.name}:{prompt.version}:{model or '-'}:{key}"


async def cached_structured[T: BaseModel](
    llm: LLMClient,
    kv: KeyValueStore | None,
    key: str,
    prompt: Prompt,
    schema: type[T],
    *,
    ttl: float,
    **values: str,
) -> LLMResult[T]:
    """`LLMClient.structured` có cache: cùng prompt (cả version), cùng model và cùng `key` (vd. hash
    nội dung tin) thì dùng lại câu trả lời cũ trong `ttl` giây. Chỉ lưu câu trả lời hợp lệ."""
    full_key = cache_key(prompt, key, llm.model)
    if kv is not None and (cached := await kv.get(full_key)) is not None:
        try:
            hit = LLMResult(
                value=schema.model_validate_json(cached),
                prompt=prompt.name,
                version=prompt.version,
                model=llm.model,
                cached=True,
            )
        except ValidationError:
            pass
        else:
            if llm.metrics is not None:
                await llm.metrics.record(hit)
            return hit
    result = await llm.structured(prompt, schema, **values)
    if kv is not None and result.value is not None:
        await kv.set(full_key, result.value.model_dump_json(), ttl=ttl)
    return result
