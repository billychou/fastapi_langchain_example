"""RBAC 查询与授权: 角色/权限加载(Redis 缓存) + 角色分配。"""
from __future__ import annotations

import json

from redis.asyncio import Redis
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.rbac import AccountRole, Permission, Role, RolePermission

_CACHE_PREFIX = "auth:rbac:"
_ALL_PERMS_WILDCARD = "*"


def _cache_key(account_id: int) -> str:
    return f"{_CACHE_PREFIX}{account_id}"


async def load_account_rbac(db: AsyncSession, account_id: int) -> tuple[list[str], set[str]]:
    """DB 加载: 角色编码列表 + 权限编码集合(admin 角色展开为通配 *)。"""
    role_rows = (
        await db.execute(
            select(Role.role_code)
            .join(AccountRole, AccountRole.role_id == Role.id)
            .where(AccountRole.account_id == account_id, Role.status == 1)
        )
    ).scalars().all()
    roles = list(role_rows)
    if "admin" in roles:  # 内置超管: 通配全部权限, 免去大表关联
        return roles, {_ALL_PERMS_WILDCARD}
    perm_rows = (
        await db.execute(
            select(Permission.perm_code)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(AccountRole, AccountRole.role_id == RolePermission.role_id)
            .where(AccountRole.account_id == account_id, Permission.status == 1)
            .distinct()
        )
    ).scalars().all()
    return roles, set(perm_rows)


async def get_account_rbac(
    db: AsyncSession, redis: Redis, account_id: int, *, refresh: bool = False
) -> tuple[list[str], set[str]]:
    """带 Redis 缓存的权限加载; 角色变更时调用 invalidate_account_rbac。"""
    s = get_settings()
    key = _cache_key(account_id)
    if not refresh:
        cached = await redis.get(key)
        if cached:
            data = json.loads(cached)
            return data["roles"], set(data["perms"])
    roles, perms = await load_account_rbac(db, account_id)
    await redis.set(
        key,
        json.dumps({"roles": roles, "perms": sorted(perms)}),
        ex=s.rbac_cache_ttl_seconds,
    )
    return roles, perms


async def invalidate_account_rbac(redis: Redis, account_id: int) -> None:
    await redis.delete(_cache_key(account_id))


async def assign_roles(
    db: AsyncSession,
    redis: Redis,
    *,
    account_id: int,
    role_codes: list[str],
    operator_id: int,
) -> list[str]:
    """全量替换账号角色(幂等): 返回最终生效的 role_codes。"""
    roles = (
        (await db.execute(select(Role).where(Role.role_code.in_(role_codes), Role.status == 1)))
        .scalars()
        .all()
    )
    found = {r.role_code for r in roles}
    missing = set(role_codes) - found
    if missing:
        from app.exceptions import BizCode, BizError

        raise BizError(BizCode.NOT_FOUND, f"角色不存在或已禁用: {sorted(missing)}")

    await db.execute(delete(AccountRole).where(AccountRole.account_id == account_id))
    db.add_all(
        [AccountRole(account_id=account_id, role_id=r.id, granted_by=operator_id) for r in roles]
    )
    await db.commit()
    await invalidate_account_rbac(redis, account_id)
    return sorted(found)
