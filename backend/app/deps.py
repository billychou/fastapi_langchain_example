"""依赖注入 + 鉴权拦截(推荐形态):

FastAPI 的依赖注入等价于传统框架的"拦截器/AOP", 且天然支持按路由声明、
OpenAPI 可见、易于测试。RBAC 校验统一走 require_permissions(...)。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core import tokens
from app.db.session import get_db
from app.exceptions import AuthError, BizCode, BizError
from app.services import rbac_service
from app.services.session_store import SessionStore


async def get_redis() -> Redis:
    from app.db.redis import get_redis_client

    client = get_redis_client()
    if client is None:
        raise BizError(BizCode.INTERNAL, "Redis 未初始化")
    return client


@dataclass(slots=True)
class AuthContext:
    """通过认证的请求上下文, 注入到业务函数。"""

    account_id: int
    session_id: str
    access_jti: str
    device_id: str | None
    roles: list[str] = field(default_factory=list)
    permissions: set[str] = field(default_factory=set)

    def has(self, perm_code: str) -> bool:
        return "*" in self.permissions or perm_code in self.permissions


def _extract_bearer(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthError(BizCode.UNAUTHORIZED, "缺少 Bearer Token")
    return token.strip()


async def get_current(
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> AuthContext:
    """解析并校验 Access Token:

    1. JWT 验签 + 过期 + issuer + typ
    2. jti 黑名单检查(登出生效)
    3. strict_session_check 时校验 Redis 会话存在(踢人/改密即时生效)
    4. 加载 RBAC 角色与权限(Redis 缓存)
    """
    s = get_settings()
    payload = tokens.decode_token(_extract_bearer(request), expected_type="access")
    account_id = int(payload["sub"])
    sid = payload["sid"]
    jti = payload["jti"]

    sessions = SessionStore(redis)
    if await sessions.is_access_blacklisted(jti):
        raise AuthError(BizCode.SESSION_REVOKED, "登录状态已失效, 请重新登录")
    if s.strict_session_check and await sessions.get_session(account_id, sid) is None:
        raise AuthError(BizCode.SESSION_REVOKED, "会话不存在或已被下线")

    roles, perms = await rbac_service.get_account_rbac(db, redis, account_id)
    return AuthContext(
        account_id=account_id,
        session_id=sid,
        access_jti=jti,
        device_id=payload.get("dev"),
        roles=roles,
        permissions=perms,
    )


async def get_current_optional(
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> AuthContext | None:
    """可选认证: CHAT_REQUIRE_AUTH=true 时等价 get_current(强制登录),
    否则返回 None(匿名演示模式)。供 /api/chat 使用。"""
    if not get_settings().chat_require_auth:
        return None
    return await get_current(request, db=db, redis=redis)


def require_permissions(*perm_codes: str):
    """路由级 RBAC 拦截器工厂:

        @router.get("/admin/accounts", dependencies=[Depends(require_permissions("account:read"))])

    需要上下文时直接作为依赖注入: ctx: AuthContext = Depends(require_permissions("rbac:assign"))
    """

    async def dependency(ctx: AuthContext = Depends(get_current)) -> AuthContext:
        missing = [c for c in perm_codes if not ctx.has(c)]
        if missing:
            raise AuthError(BizCode.FORBIDDEN, f"权限不足: 缺少 {missing}", http_status=403)
        return ctx

    return dependency
