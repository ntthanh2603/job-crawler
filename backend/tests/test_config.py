from __future__ import annotations

import pytest
from pydantic import ValidationError

from jobcrawler.config import Settings, parse_window

pytestmark = pytest.mark.usefixtures("clean_env")


def test_defaults_match_compose() -> None:
    s = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert s.database_url.startswith("postgresql+asyncpg://")
    assert ":15432/" in s.database_url
    assert s.redis_url == "redis://localhost:16379/0"
    assert (s.runner_build_concurrency, s.runner_poll_concurrency) == (10, 50)
    assert s.queue_names == ("jobcrawler-build", "jobcrawler-poll", "jobcrawler-boards")
    assert (s.build_window, s.poll_window, s.poll_interval_seconds) == (
        "00:00-06:00",
        "06:00-24:00",
        150,
    )
    assert s.gateway_port == 50051
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


def test_proxy_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    assert Settings(_env_file=None).proxy_mode == "worker"  # pyright: ignore[reportCallIssue]
    monkeypatch.setenv("PROXY_MODE", "central")
    assert Settings(_env_file=None).proxy_mode == "central"  # pyright: ignore[reportCallIssue]
    monkeypatch.setenv("PROXY_MODE", "bogus")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # pyright: ignore[reportCallIssue]


@pytest.mark.parametrize(
    ("value", "expected"),
    [("00:00-06:00", (0, 360)), ("06:00-24:00", (360, 1440)), ("22:30-05:00", (1350, 300))],
)
def test_parse_window(value: str, expected: tuple[int, int]) -> None:
    assert parse_window(value) == expected


@pytest.mark.parametrize("value", ["6-24", "25:00-26:00", "06:61-07:00"])
def test_bad_window_rejected(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("POLL_WINDOW", value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # pyright: ignore[reportCallIssue]


def test_poll_deadline_must_be_before_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLL_DEADLINE_SECONDS", "90")
    monkeypatch.setenv("POLL_TIMEOUT_SECONDS", "90")
    with pytest.raises(ValidationError, match="POLL_DEADLINE_SECONDS"):
        Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
