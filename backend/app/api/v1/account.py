"""账号自服务路由: 个人资料 / 在线会话管理(踢人)。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.net import resolve_client_ip
from app.db.session import get_db
from app.deps import AuthContext, get_current, get_redis
from app.exceptions import BizCode, BizError
from app.models.account import Account
from app.schemas import SessionInfo, UpdateLoginPolicyRequest, UpdateProfileRequest
from app.schemas.common import ok
from app.services import account_service
from app.services.auth_service import ClientMeta
from app.services.session_store import SessionStore

router = APIRouter(prefix="/account", tags=["account"])


def _meta(request: Request) -> ClientMeta:
    return ClientMeta(
        ip=resolve_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
    )


@router.get("/me", summary="当前账号资料(PII 仅返回脱敏值)")
async def me(
    ctx: AuthContext = Depends(get_current),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    account = await db.get(Account, ctx.account_id)
    if account is None:
        raise BizError(BizCode.NOT_FOUND, "账号不存在")
    return ok(await account_service.build_account_info(db, redis, account))


@router.patch("/profile", summary="更新个人资料(昵称/头像 URL)")
async def update_profile(
    payload: UpdateProfileRequest,
    request: Request,
    ctx: AuthContext = Depends(get_current),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    """自服务接口: 只能改自己的资料, 因此不引入额外权限点(有 Access Token 即可)。"""
    info = await account_service.update_profile(
        db,
        redis,
        account_id=ctx.account_id,
        nickname=payload.nickname,
        avatar_url=payload.avatar_url,
        meta=_meta(request),
    )
    return ok(info)


@router.get("/sessions", summary="当前账号在线会话列表")
async def list_sessions(
    ctx: AuthContext = Depends(get_current),
    redis: Redis = Depends(get_redis),
):
    sessions = await SessionStore(redis).list_sessions(ctx.account_id)
    data = [
        SessionInfo(
            session_id=s["sid"],
            device_id=s.get("device_id") or None,
            ip=s.get("ip"),
            user_agent=s.get("user_agent") or None,
            created_at=s.get("created_at"),
            last_seen=s.get("last_seen"),
        ).model_dump()
        | {"current": s["sid"] == ctx.session_id}
        for s in sessions
    ]
    return ok(data)


@router.delete("/sessions/{session_id}", summary="下线指定会话(单端互踢/设备管理)")
async def kick_session(
    session_id: str = Path(max_length=64),
    ctx: AuthContext = Depends(get_current),
    redis: Redis = Depends(get_redis),
):
    if session_id == ctx.session_id:
        raise BizError(BizCode.BAD_REQUEST, "当前会话请使用登出接口")
    await SessionStore(redis).revoke_session(ctx.account_id, session_id)
    return ok({"session_id": session_id})


@router.put("/login-policy", summary="切换登录策略(multi_device/single_device)")
async def update_login_policy(
    payload: UpdateLoginPolicyRequest,
    request: Request,
    ctx: AuthContext = Depends(get_current),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    """JSON body 而非 query 参数: 与其余写接口一致, 且取值校验交给 Literal。"""
    data = await account_service.set_login_policy(
        db,
        redis,
        account_id=ctx.account_id,
        session_id=ctx.session_id,
        policy=payload.policy,
        meta=_meta(request),
    )
    return ok(data)
