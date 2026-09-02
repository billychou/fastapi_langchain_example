"""认证主流程: 注册 / 登录 / 双 Token 刷新 / 登出 / 改密 / OAuth 登录。

安全约定:
- 登录失败统一返回 "账号或密码错误", 不泄露账号是否存在; 时序上以 dummy verify 对齐。
- Refresh Token 轮换 CAS 失败视为凭证泄露迹象 → 撤销会话并要求重新登录。
- 所有状态变更写审计。
"""
from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime

from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.config import get_settings
from app.core import crypto, security, tokens, validators
from app.core.ratelimit import LoginGuard, enforce_login_rate, enforce_sms_rate
from app.exceptions import AuthError, BizCode, BizError
from app.models.account import Account, UserProfile
from app.models.credential import AuthCredential
from app.models.rbac import AccountRole, Role
from app.services import audit_service
from app.services.session_store import SessionStore, new_session_id

logger = logging.getLogger("auth.service")

SMS_CODE_TTL = 300  # 验证码 5 分钟有效


@dataclass(slots=True)
class ClientMeta:
    """请求侧设备指纹, 由 API 层从 Request 中提取。"""

    ip: str
    user_agent: str | None = None
    device_id: str | None = None


@dataclass(slots=True)
class TokenPair:
    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int
    session_id: str


# ----------------------------------------------------------------------
# Token 对签发(登录/OAuth/刷新共用)
# ----------------------------------------------------------------------
async def _issue_token_pair(
    redis: Redis,
    *,
    account_id: int,
    login_policy: str,
    meta: ClientMeta,
) -> TokenPair:
    s = get_settings()
    sessions = SessionStore(redis)
    sid = new_session_id()
    access_token, _access_jti = tokens.build_token(
        account_id=account_id,
        session_id=sid,
        token_type="access",
        ttl_seconds=s.access_token_ttl_seconds,
        device_id=meta.device_id,
    )
    refresh_token, refresh_jti = tokens.build_token(
        account_id=account_id,
        session_id=sid,
        token_type="refresh",
        ttl_seconds=s.refresh_token_ttl_seconds,
        device_id=meta.device_id,
    )
    await sessions.create_session(
        account_id=account_id,
        sid=sid,
        refresh_jti=refresh_jti,
        ttl_seconds=s.refresh_token_ttl_seconds,
        device_id=meta.device_id,
        ip=meta.ip,
        user_agent=meta.user_agent,
    )
    if login_policy == "single_device":
        # 单端互踢: 新会话生效, 其余端会话被撤销(Access 因 strict_session_check 立即失效)
        await sessions.kick_other_sessions(account_id, keep_sid=sid)
    return TokenPair(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="Bearer",
        expires_in=s.access_token_ttl_seconds,
        session_id=sid,
    )


async def _get_account_for_auth(db: AsyncSession, account_id: int) -> Account:
    account = await db.get(Account, account_id)
    if account is None or account.status == 3:
        raise AuthError(BizCode.ACCOUNT_DISABLED, "账号不存在或已注销")
    if account.status == 2:
        raise AuthError(BizCode.ACCOUNT_LOCKED, "账号已被锁定, 请联系管理员")
    return account


# ----------------------------------------------------------------------
# 注册
# ----------------------------------------------------------------------
async def register(
    db: AsyncSession,
    redis: Redis,
    *,
    identity_type: str,  # phone | email
    identifier: str,
    password: str,
    nickname: str | None,
    meta: ClientMeta,
    sms_code: str | None = None,
) -> dict:
    s = get_settings()
    if identity_type == "phone":
        identifier = validators.normalize_phone(identifier)
        if s.register_require_sms:
            await _assert_sms_code(redis, identifier, sms_code)
    elif identity_type == "email":
        identifier = validators.normalize_email(identifier)
    else:
        raise BizError(BizCode.BAD_REQUEST, "不支持的注册方式")

    if (reason := security.assert_password_strength(password)) is not None:
        raise BizError(BizCode.BAD_REQUEST, reason)

    # 盲索引查重(唯一索引兜底并发)
    idx = crypto.blind_index(identifier, identity_type)
    exists = (
        await db.execute(
            select(UserProfile.id).where(
                UserProfile.phone_hash == idx if identity_type == "phone" else UserProfile.email_hash == idx
            )
        )
    ).first()
    if exists:
        raise BizError(BizCode.CREDENTIAL_ALREADY_BOUND, "该账号已注册, 请直接登录")

    account = Account(login_policy=s.login_policy_default)
    db.add(account)
    await db.flush()  # 拿 account.id

    profile_fields: dict = {"account_id": account.id}
    if identity_type == "phone":
        enc, key_id = crypto.encrypt_field(identifier)
        profile_fields.update(
            phone_enc=enc, phone_hash=idx, phone_masked=crypto.mask_phone(identifier), key_id=key_id
        )
    else:
        enc, key_id = crypto.encrypt_field(identifier)
        profile_fields.update(
            email_enc=enc, email_hash=idx, email_masked=crypto.mask_email(identifier), key_id=key_id
        )
    db.add(UserProfile(nickname=nickname or f"用户{secrets.token_hex(3)}", **profile_fields))
    db.add(
        AuthCredential(
            account_id=account.id,
            identity_type=identity_type,
            identifier=identifier,
            credential_hash=security.hash_password(password),
            verified=1,
        )
    )
    # 默认角色
    member = (await db.execute(select(Role).where(Role.role_code == "member"))).scalar_one_or_none()
    if member is not None:
        db.add(AccountRole(account_id=account.id, role_id=member.id))
    else:  # seed 未执行时兜底, 生产部署必须先跑 002_seed_rbac.sql
        logger.warning("默认角色 member 不存在, 请执行 migrations/002_seed_rbac.sql")
    await db.commit()

    await audit_service.record_audit(
        db,
        account_id=account.id,
        action="register",
        target_type="account",
        target_id=str(account.id),
        ip=meta.ip,
        user_agent=meta.user_agent,
        detail={"identity_type": identity_type},
    )
    return {"account_uuid": account.account_uuid, "nickname": nickname}


# ----------------------------------------------------------------------
# 登录
# ----------------------------------------------------------------------
async def login(
    db: AsyncSession,
    redis: Redis,
    *,
    identity_type: str,
    identifier: str,
    password: str,
    meta: ClientMeta,
    captcha_token: str | None = None,
) -> tuple[TokenPair, Account]:
    s = get_settings()
    if identity_type == "phone":
        identifier = validators.normalize_phone(identifier)
    elif identity_type == "email":
        identifier = validators.normalize_email(identifier)
    elif identity_type == "username":
        identifier = identifier.strip().lower()
    else:
        raise BizError(BizCode.BAD_REQUEST, "不支持的登录方式")

    # 1) IP + 账号维度令牌桶限流(防撞库/防刷)
    await enforce_login_rate(redis, ip=meta.ip, identifier=identifier)

    identity = f"{identity_type}:{identifier}"
    guard = LoginGuard(redis)
    # 2) 锁定检查(指数退避)
    await guard.assert_not_locked(identity)
    # 3) 连续失败后要求人机验证
    if await guard.fail_count(identity) >= s.login_captcha_threshold and not _verify_captcha(captcha_token):
        raise AuthError(BizCode.CAPTCHA_REQUIRED, "请完成人机验证后重试")

    identifier_masked = (
        crypto.mask_phone(identifier) if identity_type == "phone"
        else crypto.mask_email(identifier) if identity_type == "email"
        else f"{identifier[:2]}***"
    )

    # 4) 凭证 + 账号联查
    cred = (
        await db.execute(
            select(AuthCredential)
            .options(joinedload(AuthCredential.account))
            .where(
                AuthCredential.identity_type == identity_type,
                AuthCredential.identifier == identifier,
                AuthCredential.status == 1,
            )
        )
    ).scalar_one_or_none()

    account = cred.account if cred else None
    if account is not None and account.status != 1:
        reason = "account_locked" if account.status == 2 else "account_disabled"
        await audit_service.record_login(
            db, account_id=account.id, identity_type=identity_type, identifier_masked=identifier_masked,
            ip=meta.ip, user_agent=meta.user_agent, device_id=meta.device_id, session_id=None,
            success=False, fail_reason=reason,
        )
        raise AuthError(
            BizCode.ACCOUNT_LOCKED if account.status == 2 else BizCode.ACCOUNT_DISABLED,
            "账号状态异常, 请联系管理员",
        )

    # 5) 密码校验(账号不存在时 dummy verify 对齐时序)
    if not security.verify_password(password, cred.credential_hash if cred else None):
        await guard.register_failure(identity)
        await audit_service.record_login(
            db, account_id=account.id if account else None, identity_type=identity_type,
            identifier_masked=identifier_masked, ip=meta.ip, user_agent=meta.user_agent,
            device_id=meta.device_id, session_id=None, success=False, fail_reason="bad_password",
        )
        raise AuthError(BizCode.UNAUTHORIZED, "账号或密码错误")

    # 6) 成功路径: 清失败计数 / 参数升级回写 / 签发双 Token
    await guard.reset(identity)
    if cred and security.needs_rehash(cred.credential_hash):
        cred.credential_hash = security.hash_password(password)
    cred.last_used_at = datetime.now()
    account.last_login_at = datetime.now()
    await db.commit()

    pair = await _issue_token_pair(redis, account_id=account.id, login_policy=account.login_policy, meta=meta)
    await audit_service.record_login(
        db, account_id=account.id, identity_type=identity_type, identifier_masked=identifier_masked,
        ip=meta.ip, user_agent=meta.user_agent, device_id=meta.device_id,
        session_id=pair.session_id, success=True,
    )
    return pair, account


# ----------------------------------------------------------------------
# 无感续期: Refresh Token → 新 Access + 新 Refresh(轮换)
# ----------------------------------------------------------------------
async def refresh(db: AsyncSession, redis: Redis, *, refresh_token: str) -> TokenPair:
    s = get_settings()
    payload = tokens.decode_token(refresh_token, expected_type="refresh")
    account_id = int(payload["sub"])
    sid = payload["sid"]
    sessions = SessionStore(redis)

    session = await sessions.get_session(account_id, sid)
    if session is None:
        raise AuthError(BizCode.SESSION_REVOKED, "会话已失效, 请重新登录")

    new_refresh_token = refresh_token
    if s.refresh_token_rotation:
        new_refresh_jti = uuid.uuid4().hex
        rotated = await sessions.rotate_refresh_jti(
            account_id, sid, presented_jti=payload["jti"], new_jti=new_refresh_jti,
            ttl_seconds=s.refresh_token_ttl_seconds,
        )
        if not rotated:
            # 旧 Refresh Token 重放: 可能已被窃取 → 撤销会话, 强制重新登录
            await sessions.revoke_session(account_id, sid)
            logger.warning("refresh token reuse detected: account=%s sid=%s", account_id, sid)
            raise AuthError(BizCode.TOKEN_REUSE_DETECTED, "检测到凭证复用, 请重新登录")
        new_refresh_token = tokens.encode_refresh_with_jti(
            account_id=account_id, session_id=sid, jti=new_refresh_jti,
            ttl_seconds=s.refresh_token_ttl_seconds, device_id=payload.get("dev"),
        )

    await _get_account_for_auth(db, account_id)  # 账号状态校验(锁定/注销)
    access_token, _ = tokens.build_token(
        account_id=account_id, session_id=sid, token_type="access",
        ttl_seconds=s.access_token_ttl_seconds, device_id=payload.get("dev"),
    )
    return TokenPair(
        access_token=access_token,
        refresh_token=new_refresh_token,
        token_type="Bearer",
        expires_in=s.access_token_ttl_seconds,
        session_id=sid,
    )


# ----------------------------------------------------------------------
# 登出(支持全端)
# ----------------------------------------------------------------------
async def logout(
    redis: Redis, *, account_id: int, sid: str, access_jti: str, access_ttl_remaining: int, all_devices: bool
) -> None:
    sessions = SessionStore(redis)
    # 当前 Access Token 立即拉黑(TTL=剩余寿命, 到期自动清理)
    await sessions.blacklist_access_jti(access_jti, access_ttl_remaining)
    if all_devices:
        await sessions.revoke_all_sessions(account_id)
    else:
        await sessions.revoke_session(account_id, sid)


# ----------------------------------------------------------------------
# 修改密码: 校验旧密码 → 更新哈希 → 撤销全部会话强制重新登录
# ----------------------------------------------------------------------
async def change_password(
    db: AsyncSession, redis: Redis, *, account_id: int, old_password: str, new_password: str, meta: ClientMeta
) -> None:
    if (reason := security.assert_password_strength(new_password)) is not None:
        raise BizError(BizCode.BAD_REQUEST, reason)

    creds = (
        await db.execute(
            select(AuthCredential).where(
                AuthCredential.account_id == account_id,
                AuthCredential.credential_hash.is_not(None),
                AuthCredential.status == 1,
            )
        )
    ).scalars().all()
    if not creds or not any(security.verify_password(old_password, c.credential_hash) for c in creds):
        raise AuthError(BizCode.UNAUTHORIZED, "原密码不正确")

    new_hash = security.hash_password(new_password)
    await db.execute(
        update(AuthCredential)
        .where(AuthCredential.account_id == account_id, AuthCredential.credential_hash.is_not(None))
        .values(credential_hash=new_hash)
    )
    await db.commit()

    sessions = SessionStore(redis)
    await sessions.revoke_all_sessions(account_id)  # 全端下线
    await audit_service.record_audit(
        db, account_id=account_id, action="password_change", target_type="account",
        target_id=str(account_id), ip=meta.ip, user_agent=meta.user_agent,
    )


# ----------------------------------------------------------------------
# OAuth2 社交登录(以微信为例): 上游换取并校验 openid 后调用
# ----------------------------------------------------------------------
async def oauth_login(
    db: AsyncSession,
    redis: Redis,
    *,
    provider: str,  # wechat / apple / ...
    openid: str,
    unionid: str | None,
    nickname: str | None,
    avatar_url: str | None,
    meta: ClientMeta,
) -> tuple[TokenPair, Account]:
    identifier = f"{provider}:{openid}"
    cred = (
        await db.execute(
            select(AuthCredential)
            .options(joinedload(AuthCredential.account))
            .where(AuthCredential.identity_type == provider, AuthCredential.identifier == identifier)
        )
    ).scalar_one_or_none()

    if cred is None:
        account = Account(login_policy=get_settings().login_policy_default)
        db.add(account)
        await db.flush()
        db.add(UserProfile(account_id=account.id, nickname=nickname, avatar_url=avatar_url))
        db.add(
            AuthCredential(
                account_id=account.id, identity_type=provider, identifier=identifier,
                oauth_provider=provider, oauth_openid=openid, oauth_unionid=unionid, verified=1,
            )
        )
        member = (await db.execute(select(Role).where(Role.role_code == "member"))).scalar_one_or_none()
        if member is not None:
            db.add(AccountRole(account_id=account.id, role_id=member.id))
        await db.commit()
        await audit_service.record_audit(
            db, account_id=account.id, action="oauth_register", target_type="account",
            target_id=str(account.id), ip=meta.ip, detail={"provider": provider},
        )
    else:
        account = cred.account
        if account.status != 1:
            raise AuthError(BizCode.ACCOUNT_DISABLED, "账号状态异常")
        cred.last_used_at = datetime.now()
        await db.commit()

    pair = await _issue_token_pair(redis, account_id=account.id, login_policy=account.login_policy, meta=meta)
    await audit_service.record_login(
        db, account_id=account.id, identity_type=provider, identifier_masked=f"{provider}:***",
        ip=meta.ip, user_agent=meta.user_agent, device_id=meta.device_id,
        session_id=pair.session_id, success=True,
    )
    return pair, account


# ----------------------------------------------------------------------
# 短信验证码(演示实现): 真实环境替换为短信网关发送, 严禁在响应中回传验证码
# ----------------------------------------------------------------------
async def send_sms_code(redis: Redis, *, phone: str, meta: ClientMeta) -> None:
    phone = validators.normalize_phone(phone)
    await enforce_sms_rate(redis, ip=meta.ip, phone=phone)
    code = f"{secrets.randbelow(1_000_000):06d}"
    await redis.set(f"auth:sms:{phone}", code, ex=SMS_CODE_TTL)
    # TODO(生产): 调用短信网关发送; 记录发送日志用于合规审计
    logger.info("sms code generated for %s (dev only)", crypto.mask_phone(phone))


async def _assert_sms_code(redis: Redis, phone: str, code: str | None) -> None:
    if not code:
        raise BizError(BizCode.BAD_REQUEST, "请提供短信验证码")
    key = f"auth:sms:{phone}"
    expected = await redis.get(key)
    if expected is None:
        raise BizError(BizCode.BAD_REQUEST, "验证码已过期, 请重新获取")
    if not secrets.compare_digest(expected, code):
        raise BizError(BizCode.BAD_REQUEST, "验证码错误")
    await redis.delete(key)  # 一次性


def _verify_captcha(captcha_token: str | None) -> bool:
    """人机验证桩: 生产接入 Cloudflare Turnstile / 极验等的服务端二次校验。"""
    return captcha_token is not None and captcha_token != ""
