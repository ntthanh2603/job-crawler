from __future__ import annotations

import pytest
from pydantic import ValidationError

from jobcrawler.config import Settings

pytestmark = pytest.mark.usefixtures("clean_env")


def test_defaults_match_compose() -> None:
    s = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert s.database_url.startswith("postgresql+asyncpg://")
    assert ":15432/" in s.database_url
    assert s.redis_url == "redis://localhost:16379/0"
    assert s.worker_concurrency == 10
    assert s.browser_max_pages == 4
    assert (s.company_deadline_seconds, s.company_timeout_seconds) == (165, 180)
    assert s.llm_max_concurrency == 8
    assert not s.llm_enabled
    assert not s.serp_enabled


def test_empty_values_mean_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("SERP_API_KEY", "")
    s = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert s.llm_base_url is None
    assert s.llm_api_key.get_secret_value() == "EMPTY"
    assert s.serp_api_key is None


def test_llm_enabled_needs_url_and_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_BASE_URL", "http://vllm:8000/v1")
    assert not Settings(_env_file=None).llm_enabled  # pyright: ignore[reportCallIssue]
    monkeypatch.setenv("LLM_MODEL", "qwen")
    assert Settings(_env_file=None).llm_enabled  # pyright: ignore[reportCallIssue]


def test_deadline_must_be_before_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPANY_DEADLINE_SECONDS", "180")
    monkeypatch.setenv("COMPANY_TIMEOUT_SECONDS", "180")
    with pytest.raises(ValidationError, match="COMPANY_DEADLINE_SECONDS"):
        Settings(_env_file=None)  # pyright: ignore[reportCallIssue]


def test_secret_not_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SERP_API_KEY", "super-secret")
    s = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert s.serp_enabled
    assert "super-secret" not in repr(s)


def test_proxy_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings(_env_file=None).proxy_enabled  # pyright: ignore[reportCallIssue]
    monkeypatch.setenv("PROXY_ENABLED", "false")
    assert not Settings(_env_file=None).proxy_enabled  # pyright: ignore[reportCallIssue]
