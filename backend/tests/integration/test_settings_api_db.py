"""API trang Cài đặt và trang Workers trên Postgres + Redis thật."""

from __future__ import annotations

import contextlib
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import delete, select

from jobcrawler.bootstrap.app import create_app
from jobcrawler.common.config import Settings
from jobcrawler.database.models import AppSetting
from jobcrawler.modules.gateway import registry
from jobcrawler.modules.settings import app_settings
from jobcrawler.services import secretbox
from jobcrawler.services.redis.queues import drop_queue
from jobcrawler.services.resources import Resources

pytestmark = pytest.mark.integration

ORIGIN = {"Origin": "http://ui.test"}


@dataclass
class Web:
    client: httpx.AsyncClient
    settings: Settings
    res: Resources

    @property
    def prefix(self) -> str:
        return self.settings.redis_prefix


def by_key(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["key"]: item for g in payload["groups"] for item in g["items"]}


async def raw_rows(web: Web) -> dict[str, Any]:
    """Giá trị đúng như nằm trong bảng (trường nhạy cảm đã mã hoá)."""
    async with web.res.sessionmaker() as s:
        result = await s.execute(select(AppSetting.key, AppSetting.value))
        return dict(result.all())


async def rows(web: Web) -> dict[str, Any]:
    """Dòng của bảng (giá trị đã giải mã, người lưu)."""
    async with web.res.sessionmaker() as s:
        result = await s.execute(select(AppSetting.key, AppSetting.value, AppSetting.updated_by))
        out: dict[str, Any] = {}
        for k, v, by in result.all():
            if k in app_settings.SPEC_BY_KEY:
                value = secretbox.current().decrypt(k, v) if secretbox.is_encrypted(v) else v
                out[k] = (value, by)
        return out


@contextlib.asynccontextmanager
async def open_web(integration_settings: Settings, **env: Any) -> AsyncIterator[Web]:
    """API trên DB / Redis thật; `env`: giá trị như đặt bằng biến môi trường của backend."""
    tag = uuid4().hex[:8]
    settings = integration_settings.model_copy(
        update={
            "redis_prefix": f"jct{tag}",
            "build_queue_name": f"jobcrawler-test-build-{tag}",
            "poll_queue_name": f"jobcrawler-test-poll-{tag}",
            "board_queue_name": f"jobcrawler-test-boards-{tag}",
            "llm_base_url": None,
            "serp_api_key": None,
            "ui_password": SecretStr("mat-khau"),
        }
        | env
    )
    app = create_app(settings)
    try:
        async with app.router.lifespan_context(app):
            res: Resources = app.state.res
            async with res.sessionmaker.begin() as s:
                await s.execute(delete(AppSetting))
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://ui.test",
                headers={"Authorization": f"Bearer {app.state.token_auth.issue().token}"} | ORIGIN,
            ) as client:
                yield Web(client, settings, res)
            async with res.sessionmaker.begin() as s:
                await s.execute(delete(AppSetting))
    finally:
        redis = Redis.from_url(settings.redis_url)
        try:
            keys = [k async for k in redis.scan_iter(match=f"{settings.redis_prefix}:*")]
            if keys:
                await redis.delete(*keys)
        finally:
            await redis.aclose()
        for name in settings.queue_names:
            await drop_queue(settings.redis_url, name)


@pytest.fixture
async def web(integration_settings: Settings) -> AsyncIterator[Web]:
    async with open_web(integration_settings) as w:
        yield w


async def test_get_lists_groups_without_secrets(web: Web) -> None:
    r = await web.client.get("/api/settings")
    assert r.status_code == 200
    data = r.json()
    assert data["version"] == 0
    assert [g["group"] for g in data["groups"]] == [g.key for g in app_settings.GROUPS]
    items = by_key(data)
    assert set(items) == set(app_settings.SPEC_BY_KEY)
    assert not set(items) & app_settings.ENV_ONLY
    poll = items["poll_interval_seconds"]
    assert poll["value"] == web.settings.poll_interval_seconds
    assert poll["source"] in {"default", "env"}
    assert (poll["kind"], poll["unit"], poll["min"], poll["live"]) == ("float", "giây", 10, True)
    assert items["llm_api_key"]["value"] is None
    assert items["llm_api_key"]["secret"]
    assert web.settings.database_url not in r.text
    assert "mat-khau" not in r.text


async def test_put_saves_bumps_version_and_publishes(web: Web) -> None:
    redis = web.res.redis
    pubsub = redis.pubsub()
    await pubsub.subscribe(app_settings.channel(web.prefix))
    try:
        r = await web.client.put(
            "/api/settings",
            json={
                "values": {
                    "poll_interval_seconds": 120,
                    "rebuild_on_error": False,
                    "llm_model": "qwen-test",
                    "rate_limits": {"WWW.TopCV.vn": 0.5},
                    "boards": ["topcv", "itviec"],
                }
            },
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["version"] == 1
        assert data["restart_required"] == ["llm_model", "rate_limits"]
        # Không có `jobcrawler serve`: không tự áp dụng, không có gì "đang áp dụng".
        assert (data["auto_restart"], data["restart_pending"]) == (False, False)
        items = by_key(data)
        assert (
            items["poll_interval_seconds"]["value"],
            items["poll_interval_seconds"]["source"],
        ) == (
            120,
            "ui",
        )
        assert items["rebuild_on_error"]["value"] is False
        assert items["rate_limits"]["value"] == {"www.topcv.vn": 0.5}
        assert items["boards"]["value"] == ["topcv", "itviec"]
        message = None
        for _ in range(20):
            message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.5)
            if message:
                break
        assert message is not None
        assert message["data"] in (b"1", "1")
    finally:
        await pubsub.unsubscribe()
        await pubsub.aclose()  # pyright: ignore[reportAttributeAccessIssue]

    stored = await rows(web)
    assert stored["poll_interval_seconds"] == (120.0, "admin")
    assert stored["rate_limits"] == ({"www.topcv.vn": 0.5}, "admin")
    assert await app_settings.settings_version(redis, web.prefix) == 1
    effective = await app_settings.load_effective(web.res.sessionmaker, web.settings)
    assert (effective.poll_interval_seconds, effective.llm_model) == (120, "qwen-test")

    # Lưu lại đúng giá trị cũ của bí mật (ô ẩn) không phải thay đổi: version giữ nguyên.
    r = await web.client.put("/api/settings", json={"values": {"serp_api_key": app_settings.MASK}})
    assert r.status_code == 200
    assert r.json()["version"] == 1


async def test_restart_counter_and_state(web: Web) -> None:
    redis = web.res.redis
    restart = app_settings.restart_key(web.prefix)
    # Mục live=True: không cần khởi động lại.
    r = await web.client.put("/api/settings", json={"values": {"poll_interval_seconds": 120}})
    assert r.status_code == 200, r.text
    assert await redis.get(restart) is None
    r = await web.client.put("/api/settings", json={"values": {"llm_model": "qwen-x"}})
    assert r.json()["restart_required"] == ["llm_model"]
    assert int(await redis.get(restart)) == 1

    # `jobcrawler serve` đang chạy (khoá alive) nhưng chưa khởi động lại xong.
    await redis.set(app_settings.supervisor_key(web.prefix), 0, ex=30)
    await redis.set(app_settings.restart_applied_key(web.prefix), 0)
    data = (await web.client.get("/api/settings")).json()
    assert (data["auto_restart"], data["restart_pending"]) == (True, True)
    await redis.set(app_settings.restart_applied_key(web.prefix), 1)
    data = (await web.client.get("/api/settings")).json()
    assert (data["auto_restart"], data["restart_pending"]) == (True, False)


async def test_import_env_copies_env_values_into_db(integration_settings: Settings) -> None:
    async with open_web(
        integration_settings,
        llm_model="qwen-env",
        llm_api_key=SecretStr("sk-env"),
        board_concurrency=7,
        rate_limits={"topcv.vn": 0.5},
    ) as web:
        await web.client.put("/api/settings", json={"values": {"board_concurrency": 9}})
        page = (await web.client.get("/api/settings")).json()
        # board_concurrency đã đặt ở giao diện: giá trị giao diện thắng, không chép. (Có thể có
        # thêm key từ backend/.env của máy chạy test.)
        wanted = {"llm_api_key", "llm_model", "rate_limits"}
        assert wanted <= set(page["env_keys"])
        assert "board_concurrency" not in page["env_keys"]
        assert by_key(page)["llm_model"]["source"] == "env"

        r = await web.client.post("/api/settings/import-env")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["imported"] == page["env_keys"]
        assert (data["skipped"], data["env_keys"], data["restart_required"]) == ({}, [], [])
        items = by_key(data)
        assert items["llm_model"]["source"] == "ui"
        assert items["llm_api_key"]["is_set"] is True
        assert "sk-env" not in r.text
        stored = await rows(web)
        assert stored["llm_api_key"] == ("sk-env", "admin")
        assert stored["llm_model"] == ("qwen-env", "admin")
        assert stored["board_concurrency"] == (9, "admin")
        # Giá trị không đổi: không cần khởi động lại.
        assert await web.res.redis.get(app_settings.restart_key(web.prefix)) is None

        again = (await web.client.post("/api/settings/import-env")).json()
        assert again["imported"] == []


async def test_put_rejects_invalid_values_without_saving(web: Web) -> None:
    r = await web.client.put(
        "/api/settings",
        json={"values": {"poll_interval_seconds": 5, "rebuild_on_error": False, "boards": ["x"]}},
    )
    assert r.status_code == 422
    errors = r.json()["detail"]["errors"]
    assert set(errors) == {"poll_interval_seconds", "boards"}
    assert "10" in errors["poll_interval_seconds"]

    r = await web.client.put("/api/settings", json={"values": {"poll_deadline_seconds": 500}})
    assert r.status_code == 422
    assert set(r.json()["detail"]["errors"]) == {"poll_deadline_seconds"}

    r = await web.client.put(
        "/api/settings", json={"values": {"database_url": "postgresql://x", "khong_co": 1}}
    )
    assert r.status_code == 422
    assert set(r.json()["detail"]["errors"]) == {"database_url", "khong_co"}

    r = await web.client.put("/api/settings", json={"poll_interval_seconds": 120})
    assert r.status_code == 422  # thiếu `values`

    assert await rows(web) == {}
    assert await app_settings.settings_version(web.res.redis, web.prefix) == 0


async def test_secrets_nullable_and_reset(web: Web) -> None:
    r = await web.client.put(
        "/api/settings",
        json={"values": {"serp_api_key": "sk-bi-mat", "llm_requests_per_minute": ""}},
    )
    assert r.status_code == 200, r.text
    assert "sk-bi-mat" not in r.text
    items = by_key(r.json())
    assert (items["serp_api_key"]["is_set"], items["serp_api_key"]["source"]) == (True, "ui")
    assert items["serp_api_key"]["value"] is None
    assert items["llm_requests_per_minute"]["value"] is None
    assert items["llm_requests_per_minute"]["source"] == "ui"
    stored = await rows(web)
    assert stored["serp_api_key"][0] == "sk-bi-mat"
    assert stored["llm_requests_per_minute"][0] is None  # JSON null, khác "không có dòng"
    effective = await app_settings.load_effective(web.res.sessionmaker, web.settings)
    assert effective.serp_api_key == SecretStr("sk-bi-mat")

    r = await web.client.delete("/api/settings/serp_api_key")
    assert r.status_code == 200
    items = by_key(r.json())
    assert (items["serp_api_key"]["is_set"], items["serp_api_key"]["source"]) == (False, "env")
    assert "serp_api_key" not in await rows(web)
    assert r.json()["version"] == 2

    assert (await web.client.delete("/api/settings/database_url")).status_code == 404
    assert (await web.client.delete("/api/settings/khong_co")).status_code == 404


async def test_cross_site_write_is_blocked(web: Web) -> None:
    r = await web.client.put(
        "/api/settings",
        json={"values": {"poll_interval_seconds": 120}},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403
    assert await rows(web) == {}


async def test_workers_list_and_drain(web: Web) -> None:
    redis = web.res.redis
    keys = registry.Keys(web.prefix)
    alive = registry.WorkerStatus(
        worker_id="w-alive",
        hostname="may-1",
        version="0.1.0",
        pool="default",
        http_slots=8,
        render_slots=2,
        local_proxies=3,
        stats=registry.WorkerStats(http_inflight=3, render_inflight=1, http_done=40, http_failed=2),
    )
    gone = registry.WorkerStatus(
        worker_id="w-gone",
        pool="residential",
        http_slots=4,
        stats=registry.WorkerStats(http_done=5, render_failed=1),
    )
    await registry.save_worker(redis, keys, alive, ttl=30)
    await registry.save_worker(redis, keys, gone, ttl=30)
    # Hạn báo sống đã qua: coi như đã rời (vẫn còn thông tin trong TTL của `worker:<id>`).
    await redis.zadd(keys.workers, {"w-gone": int(time.time() * 1000) - 1000})

    data = (await web.client.get("/api/workers")).json()
    ids = [w["worker_id"] for w in data["workers"]]
    assert ids == ["w-alive", "w-gone"]
    assert [w["alive"] for w in data["workers"]] == [True, False]
    summary = data["summary"]
    assert (summary["alive"], summary["gone"], summary["draining"]) == (1, 1, 0)
    assert (summary["http_slots"], summary["http_inflight"]) == (8, 3)
    assert (summary["render_slots"], summary["render_inflight"]) == (2, 1)
    assert (summary["done"], summary["failed"]) == (45, 3)
    assert summary["pools"] == {
        "default": {
            "workers": 1,
            "http_slots": 8,
            "render_slots": 2,
            "http_inflight": 3,
            "render_inflight": 1,
        }
    }
    assert data["now_ms"] > 0

    r = await web.client.post("/api/workers/w-alive/drain")
    assert r.status_code == 200
    assert r.json() == {"worker_id": "w-alive", "draining": True}
    assert await registry.is_draining(redis, keys, "w-alive")
    data = (await web.client.get("/api/workers")).json()
    assert data["workers"][0]["draining"] is True
    assert data["summary"]["draining"] == 1

    r = await web.client.delete("/api/workers/w-alive/drain")
    assert r.status_code == 200
    assert not await registry.is_draining(redis, keys, "w-alive")

    assert (await web.client.post("/api/workers/khong-co/drain")).status_code == 404
    assert (await web.client.delete("/api/workers/khong-co/drain")).status_code == 404
    evil = await web.client.post(
        "/api/workers/w-alive/drain", headers={"Origin": "https://evil.example"}
    )
    assert evil.status_code == 403


async def test_empty_workers(web: Web) -> None:
    data = (await web.client.get("/api/workers")).json()
    assert data["workers"] == []
    assert data["summary"]["alive"] == 0
    assert data["summary"]["pools"] == {}


async def test_proxies_status_and_check(web: Web) -> None:
    lines = ["http://u%40x:p4ss@10.0.0.1:8080", "socks5://10.0.0.2:1080", "10.0.0.3:3128:bob:pw"]
    r = await web.client.put("/api/settings", json={"values": {"proxies": lines}})
    assert r.status_code == 200, r.text

    data = (await web.client.get("/api/proxies")).json()
    assert data["mode"] == web.settings.proxy_mode
    items = data["items"]
    assert [i["source"] for i in items][:3] == ["settings"] * 3
    first, second, third = items[:3]
    # `line` khớp giá trị cài đặt `proxies` (đã che mật khẩu) để giao diện ghép trạng thái.
    page = by_key((await web.client.get("/api/settings")).json())
    assert [first["line"], second["line"], third["line"]] == page["proxies"]["value"]
    assert (first["scheme"], first["host"], first["port"]) == ("http", "10.0.0.1", 8080)
    assert (first["username"], first["has_password"]) == ("u@x", True)
    assert (second["username"], second["has_password"]) == (None, False)
    assert (third["username"], third["has_password"]) == ("bob", True)
    assert "p4ss" not in str(data)
    assert "pw" not in first["line"] + third["line"]
    assert (first["score"], first["fails"], first["dead"]) == (50, 0, False)

    # Trạng thái pool trên Redis: loại proxy 1, cho nghỉ với hai domain, gắn proxy 2 với một domain.
    redis, prefix = web.res.redis, web.prefix
    await redis.set(f"{prefix}:proxy:dead:{first['id']}", "check: timeout", ex=600)
    await redis.set(f"{prefix}:proxy:fails:{first['id']}", 5)
    await redis.set(f"{prefix}:proxy:cool:{first['id']}:a.vn", 1, ex=60)
    await redis.set(f"{prefix}:proxy:cool:{first['id']}:b.vn", 1, ex=60)
    await redis.set(f"{prefix}:proxy:sticky:c.vn", second["id"], ex=60)
    data = (await web.client.get("/api/proxies")).json()
    first, second = data["items"][:2]
    assert (first["dead"], first["dead_reason"], first["fails"]) == (True, "check: timeout", 5)
    assert 0 < first["dead_seconds"] <= 600
    assert (first["cooling_domains"], first["sticky_domains"]) == (2, 0)
    assert (second["cooling_domains"], second["sticky_domains"]) == (0, 1)
    assert second["dead_seconds"] is None

    # Kiểm tra: id lạ bỏ qua; danh sách rỗng / quá dài bị từ chối.
    r = await web.client.post("/api/proxies/check", json={"ids": ["khong-co"]})
    assert r.status_code == 200, r.text
    assert r.json() == {"results": []}
    assert (await web.client.post("/api/proxies/check", json={"ids": []})).status_code == 422
    many = {"ids": [f"x{i}" for i in range(21)]}
    assert (await web.client.post("/api/proxies/check", json=many)).status_code == 422


async def test_sensitive_settings_are_encrypted_at_rest(web: Web) -> None:
    proxies = ["http://u:mat-khau-proxy@10.1.1.1:8080"]
    r = await web.client.put(
        "/api/settings", json={"values": {"serp_api_key": "sk-rat-bi-mat", "proxies": proxies}}
    )
    assert r.status_code == 200, r.text
    raw = await raw_rows(web)
    for key in ("serp_api_key", "proxies"):
        assert secretbox.is_encrypted(raw[key]), key
    assert "sk-rat-bi-mat" not in str(raw)
    assert "mat-khau-proxy" not in str(raw)
    effective = await app_settings.load_effective(web.res.sessionmaker, web.settings)
    assert effective.serp_api_key is not None
    assert effective.serp_api_key.get_secret_value() == "sk-rat-bi-mat"
    assert effective.proxies == proxies

    # Dòng cũ còn nguyên văn (trước khi có mã hoá): `seal_stored` mã hoá lại, giá trị không đổi.
    async with web.res.sessionmaker.begin() as s:
        s.add(AppSetting(key="llm_api_key", value="sk-cu", updated_by="test"))
    assert await app_settings.seal_stored(web.res.sessionmaker) == 1
    assert secretbox.is_encrypted((await raw_rows(web))["llm_api_key"])
    assert (await app_settings.load_overrides(web.res.sessionmaker))["llm_api_key"] == "sk-cu"
    assert await app_settings.seal_stored(web.res.sessionmaker) == 0

    # Mất khoá (đổi ENCRYPTION_KEYS mà bỏ khoá cũ): dòng không giải mã được thì bỏ qua.
    saved = secretbox.current()
    secretbox.configure(secretbox.SecretBox.from_secrets(["khoa-khac"]))
    try:
        assert "serp_api_key" not in await app_settings.load_overrides(web.res.sessionmaker)
    finally:
        secretbox.configure(saved)


async def test_worker_join_keys(integration_settings: Settings) -> None:
    # Gateway không hỏi khoá: không tạo khoá tham gia (bỏ GATEWAY_TOKEN đọc từ backend/.env).
    async with open_web(integration_settings, gateway_token=None) as web:
        page = (await web.client.get("/api/workers")).json()
        assert page["auth_enabled"] is False
        assert (await web.client.post("/api/workers/join-keys")).status_code == 409

    async with open_web(integration_settings, gateway_token=SecretStr("s3cret")) as web:
        page = (await web.client.get("/api/workers")).json()
        assert page["auth_enabled"] is True
        r = await web.client.post("/api/workers/join-keys")
        assert r.status_code == 200, r.text
        key = r.json()
        assert key["key"].startswith("jcj_")
        assert key["ttl_seconds"] == 900
        # Lệnh `docker run` của giao diện: không đặt địa chỉ công khai thì lấy tên máy của trang.
        assert key["gateway_url"] is None
        assert key["worker_image"] == web.settings.gateway_worker_image
        status = (await web.client.get(f"/api/workers/join-keys/{key['id']}")).json()
        assert status == {"id": key["id"], "status": "pending", "worker_id": None}
        # Redis chỉ giữ băm: khoá không nằm trong tên / giá trị khoá Redis nào.
        redis = web.res.redis
        for name in [k async for k in redis.scan_iter(match=f"{web.prefix}:gw:*")]:
            assert key["key"] not in name.decode()
            assert key["key"] not in str(await redis.get(name))
        r = await web.client.delete(f"/api/workers/join-keys/{key['id']}")
        assert r.json()["status"] == "expired"
        status = (await web.client.get(f"/api/workers/join-keys/{key['id']}")).json()
        assert status["status"] == "expired"

    async with open_web(
        integration_settings,
        gateway_token=SecretStr("s3cret"),
        gateway_public_url="https://crawler.example.com",
        gateway_worker_image="ghcr.io/acme/jobcrawler-worker:1.4.0",
    ) as web:
        key = (await web.client.post("/api/workers/join-keys")).json()
        assert key["gateway_url"] == "https://crawler.example.com"
        assert key["worker_image"] == "ghcr.io/acme/jobcrawler-worker:1.4.0"
