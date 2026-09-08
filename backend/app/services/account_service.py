"""账号自服务: 个人资料读写与登录策略。

从 API 层下沉到这里的原因:
- `/me` 与 `/profile` 需要同一份 AccountInfo 组装逻辑(避免两处漂移);
- 资料变更属于敏感操作, 必须与审计写入绑在一起, 而不是散落在路由函数里。
"""
from __future__ import annotations

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import validators
from app.exceptions import BizCode, BizError
from app.models.account import Account, UserProfile
from app.schemas import AccountInfo
from app.services import audit_service, rbac_service
from app.services.auth_service import ClientMeta
from app.services.session_store import SessionStore


async def build_account_info(
    db: AsyncSession, redis: Redis, account: Account
) -> dict:
    """组装对外资料(PII 只出脱敏值) + 角色 + 登录策略。"""
    roles, _ = await rbac_service.get_account_rbac(db, redis, account.id)
    p = account.profile
    return AccountInfo(
        account_uuid=account.account_uuid,
        nickname=p.nickname if p else None,
        avatar_url=p.avatar_url if p else None,
        phone_masked=p.phone_masked if p else None,
        email_masked=p.email_masked if p else None,
        roles=roles,
        login_policy=account.login_policy,
    ).model_dump()


async def update_profile(
    db: AsyncSession,
    redis: Redis,
    *,
    account_id: int,
    nickname: str | None,
    avatar_url: str | None,
    meta: ClientMeta,
) -> dict:
    """更新昵称/头像, 返回更新后的资料。

    语义约定:
    - `nickname=None` 不改; 传了但 strip 后为空 → BAD_REQUEST(不允许把昵称清成空白);
    - `avatar_url=None` 不改; 空串 → 置 NULL(清空头像); 非空必须是 http(s) URL;
    - 只有真正发生变化的字段才写审计, 空 body 的 no-op 请求不产生噪声日志。
    """
    # 先校验后落库: 参数非法时不留半个脏对象在 session 里
    cleaned_nickname = nickname.strip() if nickname is not None else None
    if nickname is not None and not cleaned_nickname:
        raise BizError(BizCode.BAD_REQUEST, "昵称不能为空")
    avatar_provided = avatar_url is not None
    cleaned_avatar: str | None = None
    if avatar_provided:
        cleaned_avatar = validators.normalize_avatar_url(avatar_url) or None

    account = await db.get(Account, account_id)
    if account is None:
        raise BizError(BizCode.NOT_FOUND, "账号不存在")
    profile = account.profile
    if profile is None:  # 历史数据/OAuth 首登可能没有 profile 行, 这里补建
        profile = UserProfile(account_id=account_id)
        account.profile = profile
        db.add(profile)

    changed: list[str] = []
    if cleaned_nickname is not None and cleaned_nickname != profile.nickname:
        profile.nickname = cleaned_nickname
        changed.append("nickname")
    if avatar_provided and cleaned_avatar != profile.avatar_url:
        profile.avatar_url = cleaned_avatar
        changed.append("avatar_url")

    await db.commit()
    if changed:
        await audit_service.record_audit(
            db,
            account_id=account_id,
            action="profile_update",
            target_type="account",
            target_id=str(account_id),
            ip=meta.ip,
            user_agent=meta.user_agent,
            detail={"fields": changed},
        )
    return await build_account_info(db, redis, account)


async def set_login_policy(
    db: AsyncSession,
    redis: Redis,
    *,
    account_id: int,
    session_id: str,
    policy: str,
    meta: ClientMeta,
) -> dict:
    """切换登录策略; `single_device` 立即收敛为只保留当前会话。

    策略变更是安全相关动作(会连带踢人), 因此无论取值是否真的变化都写审计,
    便于回溯"谁在什么时候把账号切成单端/多端"。
    """
    account = await db.get(Account, account_id)
    if account is None:
        raise BizError(BizCode.NOT_FOUND, "账号不存在")
    account.login_policy = policy
    await db.commit()

    kicked: list[str] = []
    if policy == "single_device":
        kicked = await SessionStore(redis).kick_other_sessions(account_id, keep_sid=session_id)

    await audit_service.record_audit(
        db,
        account_id=account_id,
        action="login_policy_change",
        target_type="account",
        target_id=str(account_id),
        ip=meta.ip,
        user_agent=meta.user_agent,
        detail={"policy": policy, "kicked_sessions": len(kicked)},
    )
    return {"login_policy": policy, "kicked_sessions": len(kicked)}
