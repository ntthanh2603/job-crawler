"""`jobcrawler.services.secretbox`: mã hoá trường nhạy cảm (AES-256-GCM), đổi khoá, khoá tự sinh."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from jobcrawler.common.config import Settings
from jobcrawler.services import secretbox
from jobcrawler.services.secretbox import DecryptError, SecretBox


def test_roundtrip_is_randomized_and_bound_to_field() -> None:
    box = SecretBox.from_secrets(["khoa-1"])
    value = ["http://u:mật-khẩu@1.2.3.4:8080"]
    a, b = box.encrypt("proxies", value), box.encrypt("proxies", value)
    assert a.startswith(f"enc:v1:{box.active}:")
    assert a != b  # nonce ngẫu nhiên: cùng giá trị ra bản mã khác nhau
    assert "mật-khẩu" not in a
    assert box.decrypt("proxies", a) == value
    with pytest.raises(DecryptError):
        box.decrypt("llm_api_key", a)  # bản mã gắn với tên trường (AAD)
    with pytest.raises(DecryptError):
        box.decrypt("proxies", a[:-4] + "AAAA")  # sửa bản mã
    with pytest.raises(DecryptError):
        box.decrypt("proxies", "nguyen-van")


def test_rotation_old_keys_decrypt_only() -> None:
    old = SecretBox.from_secrets(["cu"])
    token = old.encrypt("serp_api_key", "sk-1")
    both = SecretBox.from_secrets(["cu", "moi"])  # khoá mới ở cuối: dùng để mã hoá
    assert both.active != old.active
    assert both.decrypt("serp_api_key", token) == "sk-1"
    assert not both.is_current(token)
    assert both.is_current(both.encrypt("serp_api_key", "sk-1"))
    with pytest.raises(DecryptError, match="không có khoá"):
        SecretBox.from_secrets(["moi"]).decrypt("serp_api_key", token)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}

    async def set(self, name: str, value: Any, *, nx: bool = False) -> bool:
        if nx and name in self.values:
            return False
        self.values[name] = value
        return True

    async def get(self, name: str) -> Any:
        value = self.values.get(name)
        return value.encode() if isinstance(value, str) else value


@pytest.mark.usefixtures("clean_env")
async def test_ensure_uses_env_keys_or_shared_redis_key() -> None:
    redis = FakeRedis()
    with_keys = Settings(_env_file=None, encryption_keys=SecretStr("a, b"))  # pyright: ignore[reportCallIssue]
    box = await secretbox.ensure(with_keys, redis)
    assert box.active == SecretBox.from_secrets(["b"]).active
    assert redis.values == {}

    plain = Settings(_env_file=None, redis_prefix="jx")  # pyright: ignore[reportCallIssue]
    first = await secretbox.ensure(plain, redis)
    second = await secretbox.ensure(plain, redis)  # process khác: cùng khoá trên Redis
    assert first.active == second.active
    assert list(redis.values) == ["jx:secret:encryption_key"]
    token = first.encrypt("llm_api_key", "sk")
    assert secretbox.current().decrypt("llm_api_key", token) == "sk"
    secretbox.configure(None)
