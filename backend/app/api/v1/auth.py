"""认证路由: 注册 / 登录 / 刷新 / 登出 / 改密 / 短信验证码 / OAuth。"""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import tokens
from app.db.session import get_db
from app.deps import AuthContext, get_current, get_redis
from app.exceptions import AuthError, BizCode
from app.schemas import (
    LoginRequest,
    LogoutRequest,
    OAuthCallbackRequest,
    RefreshRequest,
    RegisterRequest,
    ChangePasswordRequest,
    SmsSendRequest,
    TokenPairResponse,
)
from app.schemas.common import ok
from app.services import auth_service
from app.services.auth_service import ClientMeta

router = APIRouter(prefix="/auth", tags=["auth"])


def _meta(request: Request, device_id: str | None = None) -> ClientMeta:
    return ClientMeta(
        ip=request.client.host if request.client else "-",
        user_agent=request.headers.get("User-Agent"),
        device_id=device_id,
    )


@router.post("/register", summary="注册(手机号/邮箱 + 密码)")
async def register(
    payload: RegisterRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    data = await auth_service.register(
        db, redis,
        identity_type=payload.identity_type,
        identifier=payload.identifier,
        password=payload.password,
        nickname=payload.nickname,
        sms_code=payload.sms_code,
        meta=_meta(request),
    )
    return ok(data)


@router.post("/login", summary="登录, 返回双 Token")
async def login(
    payload: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    pair, account = await auth_service.login(
        db, redis,
        identity_type=payload.identity_type,
        identifier=payload.identifier,
        password=payload.password,
        captcha_token=payload.captcha_token,
        meta=_meta(request, payload.device_id),
    )
    return ok(
        TokenPairResponse(
            access_token=pair.access_token,
            refresh_token=pair.refresh_token,
            token_type=pair.token_type,
            expires_in=pair.expires_in,
            session_id=pair.session_id,
        ).model_dump()
        | {"account_uuid": account.account_uuid}
    )


@router.post("/refresh", summary="Refresh Token 无感续期(自动轮换)")
async def refresh(
    payload: RefreshRequest,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    pair = await auth_service.refresh(db, redis, refresh_token=payload.refresh_token)
    return ok(TokenPairResponse(**asdict(pair)).model_dump())


@router.post("/logout", summary="登出(可选全端下线)")
async def logout(
    payload: LogoutRequest,
    request: Request,
    ctx: AuthContext = Depends(get_current),
    redis: Redis = Depends(get_redis),
):
    bearer = request.headers["Authorization"].partition(" ")[2]
    access_payload = tokens.decode_token(bearer, expected_type="access")
    await auth_service.logout(
        redis,
        account_id=ctx.account_id,
        sid=ctx.session_id,
        access_jti=ctx.access_jti,
        access_ttl_remaining=tokens.access_ttl_remaining(access_payload),
        all_devices=payload.all_devices,
    )
    return ok({"all_devices": payload.all_devices})


@router.post("/password/change", summary="修改密码(全端下线)")
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    ctx: AuthContext = Depends(get_current),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    await auth_service.change_password(
        db, redis,
        account_id=ctx.account_id,
        old_password=payload.old_password,
        new_password=payload.new_password,
        meta=_meta(request),
    )
    return ok({"message": "密码已修改, 请重新登录"})


@router.post("/sms/send", summary="发送短信验证码(限流: IP+手机号)")
async def sms_send(
    payload: SmsSendRequest,
    request: Request,
    redis: Redis = Depends(get_redis),
):
    await auth_service.send_sms_code(redis, phone=payload.phone, meta=_meta(request))
    return ok({"message": "验证码已发送"})


@router.post("/oauth/{provider}/callback", summary="OAuth2 登录/注册(演示)")
async def oauth_callback(
    provider: str,
    payload: OAuthCallbackRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    """生产环境: 此处接收 code → 服务端向 OAuth 提供方换取并校验 openid,
    严禁信任客户端直传的 openid。本演示端点假定 openid 已由上游网网校验。"""
    if provider not in {"wechat", "apple"}:
        raise AuthError(BizCode.BAD_REQUEST, "不支持的 OAuth 提供方", http_status=400)
    pair, account = await auth_service.oauth_login(
        db, redis,
        provider=provider,
        openid=payload.openid,
        unionid=payload.unionid,
        nickname=payload.nickname,
        avatar_url=payload.avatar_url,
        meta=_meta(request, payload.device_id),
    )
    return ok(
        TokenPairResponse(**asdict(pair)).model_dump() | {"account_uuid": account.account_uuid}
    )
