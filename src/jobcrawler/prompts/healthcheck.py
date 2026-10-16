"""Kiểm tra kết nối LLM + structured output (`jobcrawler llm check`)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from jobcrawler.prompts.base import Prompt


class HealthcheckAnswer(BaseModel):
    ok: bool
    nonce: str = Field(description="Chép lại đúng chuỗi nonce được cho")
    language: str = Field(description="Mã ISO 639-1 của câu được cho, ví dụ 'vi'")


PROMPT = Prompt(
    name="healthcheck",
    version="2026-10-16.1",
    system="Bạn là bộ kiểm tra kết nối. Chỉ trả về JSON đúng schema, không giải thích.",
    user=(
        "Trả về ok=true, nonce là chuỗi `$nonce` (chép nguyên văn), "
        'và language là mã ngôn ngữ ISO 639-1 của câu sau: "$sentence"'
    ),
)
