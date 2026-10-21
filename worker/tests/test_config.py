"""Cấu hình từ thư mục secret (Docker secrets): SECRETS_DIR hoặc /run/secrets."""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_settings import SettingsError

from jobworker.common import config
from jobworker.common.config import WorkerSettings
from jobworker.services.proxies import load_proxies


def settings(**kw: object) -> WorkerSettings:
    return WorkerSettings(_env_file=None, **kw)  # pyright: ignore[reportCallIssue]


def secrets_dir(tmp_path: Path, **files: str) -> Path:
    path = tmp_path / "secrets"
    path.mkdir()
    for name, text in files.items():
        (path / name).write_text(text, encoding="utf-8")
    return path


def test_secrets_dir_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proxies = (
        "# proxy tại worker, định dạng như config/proxies.example.txt\n"
        "http://u:p@1.2.3.4:8080\n"
        "\n"
        "5.6.7.8:3128:user:pass  # ghi chú, có dấu phẩy\n"
    )
    path = secrets_dir(
        tmp_path, gateway_token="bí-mật\n", WORKER_PROXIES=proxies, worker_pool="residential"
    )
    monkeypatch.setenv("SECRETS_DIR", str(path))
    s = settings()
    assert s.gateway_token is not None
    assert s.gateway_token.get_secret_value() == "bí-mật"  # bỏ xuống dòng cuối file
    # Tên file không phân biệt hoa thường; dòng chú thích / trống bị bỏ.
    assert s.worker_proxies == ["http://u:p@1.2.3.4:8080", "5.6.7.8:3128:user:pass"]
    assert [p.url for p in load_proxies(s.worker_proxies)] == [
        "http://u:p@1.2.3.4:8080",
        "http://user:pass@5.6.7.8:3128",
    ]
    assert s.worker_pool == "residential"  # biến nào cũng đọc được từ secret


def test_env_wins_over_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRETS_DIR", str(secrets_dir(tmp_path, gateway_token="từ-file")))
    monkeypatch.setenv("GATEWAY_TOKEN", "từ-env")
    s = settings()
    assert s.gateway_token is not None
    assert s.gateway_token.get_secret_value() == "từ-env"
    # Biến môi trường trống coi như không đặt: dùng secret.
    monkeypatch.setenv("GATEWAY_TOKEN", "")
    s = settings()
    assert s.gateway_token is not None
    assert s.gateway_token.get_secret_value() == "từ-file"


def test_default_run_secrets_only_if_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Không có /run/secrets (máy dev): không đọc, không cảnh báo.
    monkeypatch.setattr(config, "DEFAULT_SECRETS_DIR", tmp_path / "missing")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert settings().gateway_token is None
    monkeypatch.setattr(config, "DEFAULT_SECRETS_DIR", secrets_dir(tmp_path, gateway_token="t"))
    s = settings()
    assert s.gateway_token is not None
    assert s.gateway_token.get_secret_value() == "t"


def test_secrets_dir_from_dotenv(tmp_path: Path) -> None:
    path = secrets_dir(tmp_path, gateway_token="t")
    env_file = tmp_path / ".env"
    env_file.write_text(f"SECRETS_DIR={path}\n", encoding="utf-8")
    s = WorkerSettings(_env_file=env_file)  # pyright: ignore[reportCallIssue]
    assert s.gateway_token is not None
    assert s.gateway_token.get_secret_value() == "t"


def test_wrong_secrets_dir_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRETS_DIR", str(tmp_path / "khong-co"))
    with pytest.raises(SettingsError, match="SECRETS_DIR"):
        settings()


def test_explicit_secrets_dir_argument(tmp_path: Path) -> None:
    path = secrets_dir(tmp_path, gateway_token="t")
    s = WorkerSettings(_env_file=None, _secrets_dir=path)  # pyright: ignore[reportCallIssue]
    assert s.gateway_token is not None
    assert s.gateway_token.get_secret_value() == "t"


def test_health_file_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKER_HEALTH_FILE")
    assert settings().worker_health_file == config.default_health_file()
    assert config.default_health_file().name == "jobworker-health.json"


def test_watchdog_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    assert settings().worker_watchdog_seconds == 120
    assert settings(worker_watchdog_seconds=0).worker_watchdog_seconds == 0  # 0 = tắt
    monkeypatch.setenv("WORKER_WATCHDOG_SECONDS", "45")
    assert settings().worker_watchdog_seconds == 45
    # Hạn quá ngắn giết cả worker khoẻ; số âm vô nghĩa.
    with pytest.raises(ValidationError, match="ít nhất 10"):
        settings(worker_watchdog_seconds=5)
    with pytest.raises(ValidationError):
        settings(worker_watchdog_seconds=-1)


def test_worker_id_and_pool_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Gateway từ chối id / pool khác dạng (INVALID_ARGUMENT): báo lỗi ngay lúc khởi động."""
    s = settings(worker_id="may-1.hn:2@vn_x", worker_pool="residential.hn_1")
    assert (s.worker_id, s.worker_pool) == ("may-1.hn:2@vn_x", "residential.hn_1")
    for bad in ("máy 1", "a/b", "x" * 129):
        with pytest.raises(ValidationError, match="worker_id"):
            settings(worker_id=bad)
    for bad in ("dân cư", "a:b", "x" * 65):
        with pytest.raises(ValidationError, match="worker_pool"):
            settings(worker_pool=bad)
    # Id mặc định lấy từ hostname: ký tự lạ được thay, luôn hợp lệ.
    monkeypatch.setattr(config.socket, "gethostname", lambda: "Máy chủ#1")
    worker_id = config.default_worker_id()
    assert worker_id.startswith("M-y-ch--1-")
    assert settings(worker_id=worker_id).worker_id == worker_id
