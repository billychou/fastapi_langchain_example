"""双 Token 签发与校验: Access(短过期/无状态验签) + Refresh(长过期/服务端可撤销)。

- 两种 Token 均为 JWT, 通过 typ 区分, jti 全局唯一, sid 绑定服务端会话。
- Access 撤销依赖: (a) jti 黑名单(登出) + (b) Redis 会话存在性检查(踢人/改密)。
- Refresh 撤销依赖: 会话内 refresh_jti CAS 轮换, 天然支持重放检测。
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Literal

import jwt

from app.config import get_settings
from app.exceptions import AuthError, BizCode

TokenType = Literal["access", "refresh"]


def _now() -> int:
    return int(time.time())


def build_token(
    *,
    account_id: int,
    session_id: str,
    token_type: TokenType,
    ttl_seconds: int,
    device_id: str | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> tuple[str, str]:
    """签发 JWT, 返回 (token, jti)。"""
    s = get_settings()
    jti = uuid.uuid4().hex
    now = _now()
    payload: dict[str, Any] = {
        "iss": s.jwt_issuer,
        "sub": str(account_id),
        "jti": jti,
        "sid": session_id,
        "typ": token_type,
        "iat": now,
        "nbf": now - s.jwt_leeway_seconds,
        "exp": now + ttl_seconds,
    }
    if device_id:
        payload["dev"] = device_id
    if extra_claims:
        payload.update(extra_claims)
    token = jwt.encode(payload, s.jwt_secret_key, algorithm=s.jwt_algorithm)
    return token, jti


def decode_token(token: str, expected_type: TokenType) -> dict[str, Any]:
    """验签 + 过期 + issuer + 类型校验; 失败抛出带错误码的 AuthError。"""
    s = get_settings()
    try:
        payload = jwt.decode(
            token,
            s.jwt_secret_key,
            algorithms=[s.jwt_algorithm],
            issuer=s.jwt_issuer,
            leeway=s.jwt_leeway_seconds,
            options={"require": ["exp", "iat", "sub", "jti", "sid", "typ"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise AuthError(BizCode.TOKEN_EXPIRED, "Token 已过期, 请刷新") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError(BizCode.TOKEN_INVALID, "Token 无效") from exc
    if payload.get("typ") != expected_type:
        raise AuthError(BizCode.TOKEN_INVALID, "Token 类型错误")
    if not str(payload.get("sub", "")).isdigit():
        raise AuthError(BizCode.TOKEN_INVALID, "Token 主体非法")
    return payload


def access_ttl_remaining(payload: dict[str, Any]) -> int:
    """黑名单 TTL = 剩余有效期, 过期后黑名单条目自动清理。"""
    return max(0, int(payload["exp"] - _now()))


def encode_refresh_with_jti(
    *, account_id: int, session_id: str, jti: str, ttl_seconds: int, device_id: str | None = None
) -> str:
    """以指定 jti 编码 Refresh Token(与 Redis CAS 写入的 jti 保持一致, 只编码一次)。"""
    s = get_settings()
    now = _now()
    payload: dict[str, Any] = {
        "iss": s.jwt_issuer,
        "sub": str(account_id),
        "jti": jti,
        "sid": session_id,
        "typ": "refresh",
        "iat": now,
        "nbf": now - s.jwt_leeway_seconds,
        "exp": now + ttl_seconds,
    }
    if device_id:
        payload["dev"] = device_id
    return jwt.encode(payload, s.jwt_secret_key, algorithm=s.jwt_algorithm)
