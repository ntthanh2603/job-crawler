"""API trang Cài đặt (`/api/settings…`): giá trị gốc là `app.state.settings` (biến môi trường /
.env lúc API khởi động), đè bởi bảng `app_settings` (`modules.settings.app_settings_service`)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from jobcrawler.common.api import actor_of, res_of, settings_of
from jobcrawler.modules.settings import app_settings_service
from jobcrawler.modules.settings.settings_schema import (
    SettingsImportedOut,
    SettingsPageOut,
    SettingsSavedOut,
    SettingsUpdate,
)

router = APIRouter(prefix="/api", tags=["settings"])


async def _settings_page(request: Request) -> SettingsPageOut:
    res, base = res_of(request), settings_of(request)
    prefix = base.redis_prefix
    overrides = await app_settings_service.load_overrides(res.sessionmaker)
    effective = app_settings_service.apply_overrides(base, overrides)
    restart = await app_settings_service.restart_state(res.redis, prefix)
    return SettingsPageOut.model_validate(
        {
            "version": await app_settings_service.settings_version(res.redis, prefix),
            "groups": app_settings_service.describe(base, overrides, effective),
            "env_keys": sorted(app_settings_service.env_overrides(base, overrides)),
            "auto_restart": restart.auto,
            "restart_pending": restart.pending,
        }
    )


async def _save(request: Request, values: dict[str, Any]) -> SettingsSavedOut:
    res, base = res_of(request), settings_of(request)
    before = await app_settings_service.load_effective(res.sessionmaker, base)
    try:
        after = await app_settings_service.save(
            res.sessionmaker, res.redis, base.redis_prefix, base, values, user=actor_of(request)
        )
    except app_settings_service.SettingsError as exc:
        raise HTTPException(422, detail={"errors": exc.errors}) from exc
    page = await _settings_page(request)
    return SettingsSavedOut(
        **page.model_dump(),
        restart_required=app_settings_service.restart_required(before, after, values),
    )


@router.get("/settings")
async def get_settings(request: Request) -> SettingsPageOut:
    """Mọi cài đặt sửa được, theo nhóm: giá trị hiệu lực, mặc định, từ biến môi trường, nguồn."""
    return await _settings_page(request)


@router.put("/settings")
async def put_settings(request: Request, body: SettingsUpdate) -> SettingsSavedOut:
    """Lưu nhiều cài đặt cùng lúc (kiểm tra tất cả trước, lỗi thì không lưu gì: 422 kèm
    `detail.errors` = tên cài đặt → thông báo). `restart_required`: các cài đặt vừa đổi chỉ có
    hiệu lực sau khi khởi động lại service."""
    return await _save(request, body.values)


@router.post("/settings/import-env")
async def import_env_settings(request: Request) -> SettingsImportedOut:
    """Chép các cài đặt đang lấy từ biến môi trường / .env của backend (`env_keys`) vào DB để quản
    lý ở trang này; giá trị đang dùng không đổi. Giá trị không hợp lệ ở giao diện thì bỏ qua."""
    res, base = res_of(request), settings_of(request)
    result = await app_settings_service.import_env(
        res.sessionmaker, res.redis, base.redis_prefix, base, user=actor_of(request)
    )
    page = await _settings_page(request)
    return SettingsImportedOut(
        **page.model_dump(), restart_required=[], imported=result.keys, skipped=result.skipped
    )


@router.delete("/settings/{key}")
async def reset_setting(request: Request, key: str) -> SettingsSavedOut:
    """Bỏ giá trị đặt ở giao diện, về giá trị biến môi trường / mặc định."""
    if key not in app_settings_service.SPEC_BY_KEY:
        raise HTTPException(404, "Không có cài đặt này hoặc cài đặt chỉ đặt bằng biến môi trường")
    return await _save(request, {key: None})
