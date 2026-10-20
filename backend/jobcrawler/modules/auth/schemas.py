"""Mô hình vào / ra của API đăng nhập."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from jobcrawler.common.api import ApiOut
from jobcrawler.common.config import AuthMode


class MeOut(ApiOut):
    user: str | None
    # Cách đăng nhập: `proxy` (SSO qua proxy, header AUTH_PROXY_USER_HEADER), `jwt` (trang đăng
    # nhập, tài khoản UI_USERNAME / UI_PASSWORD), `none` (không hỏi đăng nhập).
    auth: AuthMode
    # Đường dẫn đăng xuất (AUTH_LOGOUT_URL, vd. /oauth2/sign_out); null = không có nút đăng xuất.
    logout_url: str | None


class LoginIn(BaseModel):
    username: str = Field(max_length=200)
    password: str = Field(max_length=1000)


class LoginOut(ApiOut):
    user: str
    # Cũng nằm trong cookie httpOnly `jc_token`; script gọi API gửi `Authorization: Bearer <token>`.
    access_token: str
    token_type: Literal["bearer"]
    expires_at: datetime


class LogoutOut(ApiOut):
    ok: bool
