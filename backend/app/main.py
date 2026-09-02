"""FastAPI entrypoint: SSE streaming chat + 用户认证/RBAC 一体化服务。

- /api/health       健康检查(匿名)
- /api/chat         SSE 聊天(默认要求登录, CHAT_REQUIRE_AUTH 控制), 实现见 app/api/chat.py
- /api/v1/auth/*    注册/登录/刷新/登出/改密/OAuth
- /api/v1/account/* 当前用户资料与在线会话
- /api/v1/admin/*   管理端(RBAC 权限点控制)
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.agent import close_checkpointer, init_checkpointer
from app.api.chat import router as chat_router
from app.api.v1.router import api_router
from app.config import get_settings
from app.db.redis import close_redis, init_redis
from app.db.session import dispose_engine, get_engine
from app.exceptions import BizCode, BizError
from app.middleware.auth import build_middlewares

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
logger = logging.getLogger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """预热资源: 数据库引擎惰性创建; Redis 探活失败仅告警(聊天功能可降级运行)。"""
    get_engine()
    await init_checkpointer()  # 打开 checkpointer 连接并建表
    try:
        redis = init_redis()
        await redis.ping()
    except Exception as exc:  # noqa: BLE001 - Redis 不可用时聊天仍可降级服务
        logger.warning("Redis 不可用, 认证相关接口将无法工作: %s", exc)
    logger.info("backend started")
    yield
    await close_checkpointer()
    await close_redis()
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="LangChain Agent Chat API",
        version="0.2.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.app_env != "production" or settings.debug else None,
        redoc_url=None,
    )

    build_middlewares(app)  # RequestId → 安全头 → (可选)全局RBAC
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    logger.info("CORS allowed origins: %s", settings.cors_origin_list)

    # ---------------- 统一异常信封 ----------------
    @app.exception_handler(BizError)
    async def biz_error_handler(request: Request, exc: BizError) -> JSONResponse:
        return JSONResponse(
            {"code": int(exc.code), "message": exc.message, "data": exc.data},
            status_code=exc.http_status,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        loc = ".".join(str(x) for x in first.get("loc", []) if x != "body")
        msg = f"参数错误: {loc} {first.get('msg', '')}".strip()
        return JSONResponse(
            {"code": int(BizCode.BAD_REQUEST), "message": msg, "data": None}, status_code=400
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled exception: %s %s", request.method, request.url.path)
        return JSONResponse(
            {"code": int(BizCode.INTERNAL), "message": "服务内部错误", "data": None}, status_code=500
        )

    # ---------------- 基础设施 ----------------
    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "provider": settings.llm_provider,
            "model": settings.llm_model,
        }

    # ---------------- 路由 ----------------
    app.include_router(chat_router, prefix="/api")  # /api/chat: SSE 聊天
    app.include_router(api_router, prefix=settings.api_prefix)
    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=5001)
