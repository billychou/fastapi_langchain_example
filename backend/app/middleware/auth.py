"""中间件样例集:

1. RequestContextMiddleware — 注入 request-id、访问日志与耗时统计
2. SecurityHeadersMiddleware — 安全响应头
3. JwtPermissionMiddleware  — 网关式全局 JWT+RBAC 拦截(演示形态, 默认关闭)

FastAPI 工程实践首选 deps.require_permissions 依赖注入式鉴权;
JwtPermissionMiddleware 展示的是"网关/全局拦截器"形态, 适合存量路由统一收口。
"""
from __future__ import annotations

import logging
import time
import uuid
from fnmatch import fnmatch

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.config import get_settings
from app.core.net import resolve_client_ip

logger = logging.getLogger("auth.http")

REQUEST_ID_HEADER = "X-Request-Id"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """生成 request-id 并记录访问日志(可对接 OpenTelemetry trace)。"""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        request.state.request_id = request_id
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "unhandled error: %s %s rid=%s", request.method, request.url.path, request_id
            )
            raise
        cost_ms = (time.perf_counter() - start) * 1000
        response.headers[REQUEST_ID_HEADER] = request_id
        logger.info(
            "%s %s -> %d (%.1fms) ip=%s rid=%s",
            request.method, request.url.path, response.status_code, cost_ms,
            resolve_client_ip(request), request_id,
        )
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """基线安全响应头; 认证相关路径强制 no-store 防缓存敏感数据。"""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if "/auth" in request.url.path:
            response.headers.setdefault("Cache-Control", "no-store")
            response.headers.setdefault("Pragma", "no-cache")
        return response


class JwtPermissionMiddleware(BaseHTTPMiddleware):
    """全局路由→权限拦截器(网关式演示)。

    - 白名单路径直接放行(健康检查/登录/注册等匿名接口)
    - 其余路径按 route-permission 映射表校验 Access Token 与权限
    - 生产建议: 权限映射表落库(permission.api_method/api_path), 此处硬编码仅演示

    默认关闭; 开启: ENABLE_GLOBAL_RBAC_MIDDLEWARE=true
    """

    PUBLIC_PATHS = (
        "/api/health",
        "/api/v1/auth/login",
        "/api/v1/auth/register",
        "/api/v1/auth/refresh",
        "/api/v1/auth/sms/send",
        "/api/v1/auth/oauth/*",
        "/api/chat",  # 聊天接口自带依赖注入鉴权, 全局中间件不重复校验
        "/docs",
        "/openapi.json",
    )

    ROUTE_PERMISSIONS: dict[str, str] = {
        "GET /api/v1/admin/accounts": "account:read",
        "POST /api/v1/admin/accounts/*/roles": "rbac:assign",
    }

    def __init__(self, app, *, enabled: bool = False) -> None:  # noqa: ANN001
        super().__init__(app)
        self._enabled = enabled

    def _is_public(self, path: str) -> bool:
        return any(fnmatch(path, p) for p in self.PUBLIC_PATHS)

    def _match_permission(self, method: str, path: str) -> str | None:
        for route, perm in self.ROUTE_PERMISSIONS.items():
            m, _, p = route.partition(" ")
            if m == method and fnmatch(path, p):
                return perm
        return None

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if not self._enabled or self._is_public(request.url.path):
            return await call_next(request)

        from app.core import tokens
        from app.db.redis import get_redis_client
        from app.db.session import get_session_factory
        from app.exceptions import AuthError, BizCode
        from app.services import rbac_service
        from app.services.session_store import SessionStore

        def deny(code: int, biz: int, message: str) -> JSONResponse:
            return JSONResponse({"code": biz, "message": message, "data": None}, status_code=code)

        header = request.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token:
            return deny(401, BizCode.UNAUTHORIZED, "缺少 Bearer Token")
        try:
            payload = tokens.decode_token(token, expected_type="access")
        except AuthError as exc:
            return deny(exc.http_status, int(exc.code), exc.message)

        redis = get_redis_client()
        sessions = SessionStore(redis)
        account_id, sid, jti = int(payload["sub"]), payload["sid"], payload["jti"]
        if await sessions.is_access_blacklisted(jti) or await sessions.get_session(account_id, sid) is None:
            return deny(401, BizCode.SESSION_REVOKED, "登录状态已失效")

        perm_required = self._match_permission(request.method, request.url.path)
        if perm_required:
            factory = get_session_factory()
            async with factory() as db:
                roles, perms = await rbac_service.get_account_rbac(db, redis, account_id)
            if "*" not in perms and perm_required not in perms:
                return deny(403, BizCode.FORBIDDEN, f"权限不足: 缺少 {perm_required}")

        request.state.account_id = account_id
        return await call_next(request)


def build_middlewares(app) -> None:  # noqa: ANN001
    """统一注册中间件(注意: FastAPI 中间件执行顺序与注册顺序相反)。"""
    s = get_settings()
    app.add_middleware(JwtPermissionMiddleware, enabled=False)  # 演示用, 生产按需开启
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)
    _ = s
