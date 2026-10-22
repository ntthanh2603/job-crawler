from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from jobcrawler.common import config
from jobcrawler.common.config import Settings, parse_window

pytestmark = pytest.mark.usefixtures("clean_env")


def test_defaults_match_compose() -> None:
    s = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert s.database_url.startswith("postgresql+asyncpg://")
    assert ":15432/" in s.database_url
    assert s.redis_url == "redis://127.0.0.1:16379/0"
    # Số tối đa mỗi process; dưới mức đó runner đi theo worker đang kết nối.
    assert (s.runner_build_concurrency, s.runner_poll_concurrency) == (25, 60)
    assert s.llm_requests_per_minute == 60
    assert s.queue_names == ("jobcrawler-build", "jobcrawler-poll", "jobcrawler-boards")
    assert (s.build_window, s.poll_window, s.poll_interval_seconds) == (
        "00:00-06:00",
        "00:00-24:00",
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


# --- file bí mật (Docker secrets) ------------------------------------------------------------


def _secrets(tmp_path: Path, **files: str) -> Path:
    folder = tmp_path / "secrets"
    folder.mkdir()
    for name, value in files.items():
        (folder / name).write_text(value, encoding="utf-8")
    return folder


def test_secrets_dir_files_fill_fields(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    folder = _secrets(
        tmp_path,
        gateway_token="tok-file\n",
        UI_PASSWORD="mật khẩu",
        database_url="postgresql+asyncpg://u:p@db:5432/jc\n",
        fetch_routes='{"topcv.vn": "residential"}',
    )
    monkeypatch.setenv("SECRETS_DIR", str(folder))
    s = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert s.gateway_token is not None
    assert s.gateway_token.get_secret_value() == "tok-file"  # bỏ xuống dòng cuối file
    assert s.ui_password is not None
    assert s.ui_password.get_secret_value() == "mật khẩu"  # tên file không phân biệt hoa thường
    assert s.database_url == "postgresql+asyncpg://u:p@db:5432/jc"
    assert s.fetch_routes == {"topcv.vn": "residential"}  # kiểu phức tạp: JSON như biến môi trường
    assert s.serp_api_key is None  # không có file: mặc định


def test_env_and_init_win_over_secret_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """pydantic-settings: init > biến môi trường > .env > file bí mật > mặc định."""
    folder = _secrets(tmp_path, gateway_token="tok-file", serp_api_key="serp-file")
    env_file = tmp_path / ".env"
    env_file.write_text("SERP_API_KEY=serp-dotenv\n", encoding="utf-8")
    monkeypatch.setenv("SECRETS_DIR", str(folder))
    monkeypatch.setenv("GATEWAY_TOKEN", "tok-env")
    s = Settings(_env_file=env_file)  # pyright: ignore[reportCallIssue]
    assert s.gateway_token is not None
    assert s.gateway_token.get_secret_value() == "tok-env"
    assert s.serp_api_key is not None
    assert s.serp_api_key.get_secret_value() == "serp-dotenv"
    init = Settings(_env_file=None, gateway_token="tok-init")  # pyright: ignore[reportCallIssue]
    assert init.gateway_token is not None
    assert init.gateway_token.get_secret_value() == "tok-init"
    # Biến môi trường rỗng bị bỏ qua (env_ignore_empty): file bí mật vẫn được dùng.
    monkeypatch.setenv("GATEWAY_TOKEN", "")
    s = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert s.gateway_token is not None
    assert s.gateway_token.get_secret_value() == "tok-file"


def test_secrets_dir_default_only_if_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    folder = _secrets(tmp_path, gateway_token="tok-default")
    monkeypatch.delenv("SECRETS_DIR", raising=False)
    monkeypatch.setattr(config, "DEFAULT_SECRETS_DIR", tmp_path / "khong-co")
    assert (
        config.secrets_dir() is None
    )  # máy dev không có /run/secrets: không đọc gì, không cảnh báo
    assert Settings(_env_file=None).gateway_token is None  # pyright: ignore[reportCallIssue]
    monkeypatch.setattr(config, "DEFAULT_SECRETS_DIR", folder)
    assert config.secrets_dir() == folder
    s = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert s.gateway_token is not None
    assert s.gateway_token.get_secret_value() == "tok-default"
    # SECRETS_DIR rỗng: tắt hẳn, kể cả khi /run/secrets có.
    monkeypatch.setenv("SECRETS_DIR", "")
    assert config.secrets_dir() is None
    assert Settings(_env_file=None).gateway_token is None  # pyright: ignore[reportCallIssue]
    # Truyền thẳng `_secrets_dir` thắng SECRETS_DIR.
    explicit = Settings(_env_file=None, _secrets_dir=folder)  # pyright: ignore[reportCallIssue]
    assert explicit.gateway_token is not None


def test_auth_mode_and_cache_redis_url(monkeypatch: pytest.MonkeyPatch) -> None:
    s = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert (s.auth_mode, s.effective_cache_redis_url) == ("none", s.redis_url)
    monkeypatch.setenv("UI_PASSWORD", "pw")
    monkeypatch.setenv("CACHE_REDIS_URL", "")  # rỗng = dùng REDIS_URL
    s = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert (s.auth_mode, s.effective_cache_redis_url) == ("jwt", s.redis_url)
    monkeypatch.setenv("AUTH_PROXY_USER_HEADER", "X-Auth-Request-User")
    monkeypatch.setenv("CACHE_REDIS_URL", "redis://cache:6379/0")
    s = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert (s.auth_mode, s.effective_cache_redis_url) == ("proxy", "redis://cache:6379/0")


def test_build_deadline_leaves_time_to_save() -> None:
    """Lượt build luôn chừa thời gian lưu trước timeout cứng: lượt bị huỷ thì mất hết tin."""
    from jobcrawler.modules.crawl.company_service import build_deadline

    s = Settings(_env_file=None, company_deadline_seconds=165, company_timeout_seconds=180)  # pyright: ignore[reportCallIssue]
    assert build_deadline(s, 0.0) == 150.0
    quick = Settings(_env_file=None, company_deadline_seconds=2, company_timeout_seconds=4)  # pyright: ignore[reportCallIssue]
    assert build_deadline(quick, 10.0) == 12.0
