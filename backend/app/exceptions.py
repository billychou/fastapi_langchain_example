"""统一业务错误码与异常体系: 所有对外响应遵循 {code, message, data} 信封。"""
from __future__ import annotations

from enum import IntEnum
from typing import Any


class BizCode(IntEnum):
    OK = 0
    BAD_REQUEST = 40000
    UNAUTHORIZED = 40100
    TOKEN_EXPIRED = 40101
    TOKEN_INVALID = 40102
    SESSION_REVOKED = 40103
    TOKEN_REUSE_DETECTED = 40104
    FORBIDDEN = 40300
    NOT_FOUND = 40400
    CONFLICT = 40900
    CREDENTIAL_ALREADY_BOUND = 40901
    ACCOUNT_DISABLED = 40302
    ACCOUNT_LOCKED = 42301
    TOO_MANY_REQUESTS = 42900
    CAPTCHA_REQUIRED = 44001
    INTERNAL = 50000


_DEFAULT_HTTP_STATUS: dict[BizCode, int] = {
    BizCode.BAD_REQUEST: 400,
    BizCode.UNAUTHORIZED: 401,
    BizCode.TOKEN_EXPIRED: 401,
    BizCode.TOKEN_INVALID: 401,
    BizCode.SESSION_REVOKED: 401,
    BizCode.TOKEN_REUSE_DETECTED: 401,
    BizCode.FORBIDDEN: 403,
    BizCode.ACCOUNT_DISABLED: 403,
    BizCode.NOT_FOUND: 404,
    BizCode.CONFLICT: 409,
    BizCode.CREDENTIAL_ALREADY_BOUND: 409,
    BizCode.ACCOUNT_LOCKED: 423,
    BizCode.TOO_MANY_REQUESTS: 429,
    BizCode.CAPTCHA_REQUIRED: 403,
    BizCode.INTERNAL: 500,
}


class BizError(Exception):
    """业务异常基类: 携带业务错误码、用户可读消息与可选 HTTP 头(如 Retry-After)。"""

    def __init__(
        self,
        code: BizCode,
        message: str,
        *,
        http_status: int | None = None,
        headers: dict[str, str] | None = None,
        data: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status or _DEFAULT_HTTP_STATUS.get(code, 400)
        self.headers = headers or {}
        self.data = data


class AuthError(BizError):
    """认证域异常(默认 401)。"""

    def __init__(self, code: BizCode, message: str, **kwargs: Any) -> None:
        kwargs.setdefault("http_status", _DEFAULT_HTTP_STATUS.get(code, 401))
        super().__init__(code, message, **kwargs)


class RateLimitError(BizError):
    """限流异常(429), 支持 Retry-After。"""

    def __init__(self, message: str = "请求过于频繁, 请稍后再试", retry_after: int | None = None) -> None:
        headers = {"Retry-After": str(retry_after)} if retry_after else {}
        super().__init__(BizCode.TOO_MANY_REQUESTS, message, http_status=429, headers=headers)
