"""密码哈希: Argon2id (OWASP 2024 首选算法), 含时序均衡与参数升级支持。"""
from __future__ import annotations

import secrets
from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error, InvalidHashError

from app.config import get_settings


@lru_cache
def _hasher() -> PasswordHasher:
    s = get_settings()
    return PasswordHasher(
        time_cost=s.argon2_time_cost,
        memory_cost=s.argon2_memory_cost_kib,
        parallelism=s.argon2_parallelism,
        hash_len=32,
        salt_len=16,
    )


def hash_password(plain: str) -> str:
    """生成 Argon2id 哈希(自动加盐, 盐与参数编码进哈希串)。"""
    return _hasher().hash(plain)


_DUMMY_HASH: str | None = None


def _dummy_verify(plain: str) -> None:
    """账号不存在时执行一次等耗时校验, 防止通过响应时延枚举有效账号。"""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = _hasher().hash(secrets.token_urlsafe(16))
    try:
        _hasher().verify(_DUMMY_HASH, plain)
    except Argon2Error:
        pass


def verify_password(plain: str, hashed: str | None) -> bool:
    """恒定失败语义: 哈希缺失(账号不存在/无密码凭证)也消耗一次等价计算。"""
    if not hashed:
        _dummy_verify(plain)
        return False
    try:
        return _hasher().verify(hashed, plain)
    except (Argon2Error, InvalidHashError, ValueError):
        return False


def needs_rehash(hashed: str) -> bool:
    """参数升级后, 登录成功时顺带回写更强的哈希。"""
    try:
        return _hasher().check_needs_rehash(hashed)
    except (Argon2Error, InvalidHashError, ValueError):
        return True


def assert_password_strength(password: str) -> str | None:
    """返回 None 表示通过; 否则返回拒绝原因。生产可接入 zxcvbn 类强度评估。"""
    if len(password) < 8 or len(password) > 128:
        return "密码长度必须在 8-128 位之间"
    classes = sum(
        1
        for check in (str.islower, str.isupper, str.isdigit)
        if any(check(c) for c in password)
    )
    if any(not c.isalnum() for c in password):
        classes += 1
    if classes < 3:
        return "密码必须至少包含大写字母、小写字母、数字、特殊字符中的三类"
    return None
