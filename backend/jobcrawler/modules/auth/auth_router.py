"""API đăng nhập: `/api/me`, `/api/auth/login`, `/api/auth/logout`."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request, Response

from jobcrawler.common.api import res_of, user_of
from jobcrawler.common.config import Settings
from jobcrawler.common.logging import get_logger
from jobcrawler.modules.audit import audit_service
from jobcrawler.modules.auth import auth_service
from jobcrawler.modules.auth.auth_schema import LoginIn, LoginOut, LogoutOut, MeOut

router = APIRouter(prefix="/api", tags=["auth"])

log = get_logger(__name__)


async def _audit(request: Request, **event: Any) -> None:
    """Ghi nhật ký đăng nhập / đăng xuất; chưa có DB (app chưa mở lifespan) thì bỏ qua — đăng
    nhập không bao giờ hỏng vì nhật ký."""
    sessionmaker = getattr(getattr(request.app.state, "res", None), "sessionmaker", None)
    if sessionmaker is not None:
        await audit_service.record_safely(sessionmaker, **event)


@router.get("/me")
async def get_me(request: Request) -> MeOut:
    settings: Settings = request.app.state.settings
    return MeOut(
        user=user_of(request),
        auth=settings.auth_mode,
        logout_url=(settings.auth_logout_url or "").strip() or None,
    )


@router.post("/auth/login")
async def login(request: Request, response: Response, body: LoginIn) -> LoginOut:
    """Đăng nhập bằng UI_USERNAME / UI_PASSWORD: trả JWT và đặt cookie httpOnly `jc_token`. Sai
    20 lần trong 5 phút từ một IP thì 429 tới khi hết 5 phút (kể cả khi nhập đúng)."""
    token_auth: auth_service.TokenAuth | None = request.app.state.token_auth
    if token_auth is None:
        raise HTTPException(
            400, "Không bật đăng nhập bằng mật khẩu (chưa đặt UI_PASSWORD hoặc dùng SSO)"
        )
    throttle = request.app.state.throttle
    ip = request.client.host if request.client else "unknown"
    if await throttle.failures(ip) >= throttle.limit:
        retry = await throttle.retry_after(ip)
        raise HTTPException(
            429, "Đăng nhập sai quá nhiều lần, thử lại sau", headers={"Retry-After": str(retry)}
        )
    if not token_auth.check_password(body.username, body.password):
        count = await throttle.failed(ip)
        if count == throttle.limit:
            log.warning("auth.locked", ip=ip, failures=count, seconds=throttle.window)
        # Người thao tác = tên đăng nhập đã nhập (chưa chắc là tài khoản có thật); không ghi mật
        # khẩu.
        await _audit(
            request,
            action=audit_service.AUTH_LOGIN_FAILED,
            actor=body.username.strip() or audit_service.ANONYMOUS,
            resource_type="session",
            metadata={"failures": count, "locked": count >= throttle.limit},
        )
        raise HTTPException(401, "Sai tên đăng nhập hoặc mật khẩu")
    await throttle.succeeded(ip)
    issued = token_auth.issue()
    response.set_cookie(
        auth_service.COOKIE,
        issued.token,
        max_age=token_auth.ttl,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )
    log.info("auth.login", user=token_auth.username, ip=ip)
    await _audit(
        request,
        action=audit_service.AUTH_LOGIN,
        actor=token_auth.username,
        resource_type="session",
        resource_id=issued.jti,
        metadata={"expires_at": datetime.fromtimestamp(issued.expires_at, UTC).isoformat()},
    )
    return LoginOut(
        user=token_auth.username,
        access_token=issued.token,
        token_type="bearer",
        expires_at=datetime.fromtimestamp(issued.expires_at, UTC),
    )


@router.post("/auth/logout")
async def logout(request: Request, response: Response) -> LogoutOut:
    """Đăng xuất: thu hồi token đang dùng (tới khi nó hết hạn) và xoá cookie."""
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host", ""):
        raise HTTPException(403, "Request từ site khác bị chặn")
    token_auth: auth_service.TokenAuth | None = request.app.state.token_auth
    token = auth_service.token_from(request.headers)
    claims = token_auth.verify(token) if token_auth and token else None
    if claims is not None:
        settings: Settings = request.app.state.settings
        await auth_service.revoke(res_of(request).redis, settings.redis_prefix, claims)
        log.info("auth.logout", user=claims.user)
        await _audit(
            request,
            action=audit_service.AUTH_LOGOUT,
            actor=claims.user,
            resource_type="session",
            resource_id=claims.jti,
        )
    response.delete_cookie(auth_service.COOKIE, path="/")
    return LogoutOut(ok=claims is not None)
