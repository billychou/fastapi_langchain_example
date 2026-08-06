"""管理端路由(RBAC 演示): 全部接口通过 require_permissions 声明式鉴权。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.deps import AuthContext, get_redis, require_permissions
from app.exceptions import BizCode, BizError
from app.models.account import Account, UserProfile
from app.schemas.common import ok
from app.services import audit_service, rbac_service

router = APIRouter(prefix="/admin", tags=["admin"])


class AssignRolesRequest(BaseModel):
    role_codes: list[str] = Field(min_length=1, description="全量替换的角色编码列表")


@router.get(
    "/accounts",
    summary="账号列表(需 account:read)",
    dependencies=[Depends(require_permissions("account:read"))],
)
async def list_accounts(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    total = (await db.execute(select(func.count(Account.id)))).scalar_one()
    rows = (
        await db.execute(
            select(Account, UserProfile)
            .join(UserProfile, UserProfile.account_id == Account.id, isouter=True)
            .order_by(Account.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    return ok(
        {
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": [
                {
                    "account_uuid": acc.account_uuid,
                    "status": acc.status,
                    "login_policy": acc.login_policy,
                    "nickname": prof.nickname if prof else None,
                    "phone_masked": prof.phone_masked if prof else None,
                    "email_masked": prof.email_masked if prof else None,
                    "last_login_at": acc.last_login_at.isoformat() if acc.last_login_at else None,
                    "created_at": acc.created_at.isoformat() if acc.created_at else None,
                }
                for acc, prof in rows
            ],
        }
    )


@router.post("/accounts/{account_uuid}/roles", summary="分配角色(需 rbac:assign)")
async def assign_roles(
    account_uuid: str,
    payload: AssignRolesRequest,
    request: Request,
    ctx: AuthContext = Depends(require_permissions("rbac:assign")),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
):
    account = (
        await db.execute(select(Account).where(Account.account_uuid == account_uuid))
    ).scalar_one_or_none()
    if account is None:
        raise BizError(BizCode.NOT_FOUND, "账号不存在")

    final_roles = await rbac_service.assign_roles(
        db, redis, account_id=account.id, role_codes=payload.role_codes, operator_id=ctx.account_id
    )
    await audit_service.record_audit(
        db,
        account_id=ctx.account_id,
        action="role_grant",
        target_type="account",
        target_id=str(account.id),
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("User-Agent"),
        detail={"roles": final_roles},
    )
    return ok({"account_uuid": account_uuid, "roles": final_roles})
