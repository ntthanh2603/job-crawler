"""Mô hình vào / ra của API trang Cài đặt."""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, WithJsonSchema

from jobcrawler.common.api import ApiOut
from jobcrawler.modules.settings import app_settings


class SettingsUpdate(BaseModel):
    # Tên cài đặt → giá trị mới; `null` = về giá trị biến môi trường / mặc định.
    values: dict[str, Any]


# Giá trị một cài đặt như API trả (`app_settings._display`: JSON của field, bí mật luôn null, proxy
# đã ẩn mật khẩu). Khi chạy không kiểm lại; spec liệt kê các dạng có thể có.
type SettingValue = Annotated[
    Any,
    WithJsonSchema(
        {
            "anyOf": [
                {"type": "string"},
                {"type": "number"},
                {"type": "boolean"},
                {"type": "array", "items": {"type": "string"}},
                {
                    "type": "object",
                    "additionalProperties": {"anyOf": [{"type": "string"}, {"type": "number"}]},
                },
                {"type": "null"},
            ]
        }
    ),
]


class SettingOptionOut(ApiOut):
    value: str
    label: str
    help: str | None


class SettingSectionOut(ApiOut):
    """Mục con trong nhóm: tiêu đề, giải thích ngắn, các cài đặt theo thứ tự hiển thị."""

    title: str
    description: str
    keys: list[str]


class SettingItemOut(ApiOut):
    key: str
    label: str
    help: str
    kind: app_settings.Kind
    options: list[SettingOptionOut]
    unit: str | None
    min: float | None
    max: float | None
    min_exclusive: bool
    max_exclusive: bool
    nullable: bool  # để trống được (null)
    placeholder: str | None
    value_type: str | None  # kiểu giá trị của bảng `dict` ("str" | "float")
    live: bool  # False: chỉ có hiệu lực sau khi khởi động lại process (`jobcrawler serve` tự làm)
    value: SettingValue  # bí mật luôn null (xem `is_set`)
    default: SettingValue
    env_value: SettingValue
    source: app_settings.Source  # giá trị đang dùng lấy từ đâu
    secret: bool
    is_set: bool | None  # chỉ với bí mật
    env_is_set: bool | None
    # Cài đặt mà 0 = tự tính theo máy: giá trị tự động đó (trên máy chạy backend); null = không có.
    auto_value: int | None


class SettingGroupOut(ApiOut):
    group: str
    title: str
    description: str
    sections: list[SettingSectionOut]
    items: list[SettingItemOut]


class SettingsPageOut(ApiOut):
    version: int  # tăng mỗi lần lưu; 0 khi chưa lưu lần nào
    groups: list[SettingGroupOut]
    # Cài đặt đang lấy từ biến môi trường / .env (khác mặc định), chưa lưu trong DB.
    env_keys: list[str]
    # Backend chạy dưới `jobcrawler serve`: cài đặt `live=False` được tự áp dụng (khởi động lại
    # process con); False = phải khởi động lại backend.
    auto_restart: bool
    # Đã lưu cài đặt `live=False` mà backend chưa khởi động lại xong (chỉ khi auto_restart).
    restart_pending: bool


class SettingsSavedOut(SettingsPageOut):
    restart_required: list[str]  # cài đặt vừa đổi chỉ có hiệu lực sau khi khởi động lại


class SettingsImportedOut(SettingsSavedOut):
    imported: list[str]  # đã chép từ biến môi trường vào DB
    skipped: dict[str, str]  # không hợp lệ ở giao diện: key → lý do (vẫn lấy từ biến môi trường)
