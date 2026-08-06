"""应用入口: uvicorn app.main:app --reload --port 8000"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

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
logger = logging.getLogger("auth.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_engine()  # 预热连接池配置(惰性创建, 首次请求真正建连)
    redis = init_redis()
    await redis.ping()  # 启动期探活: Redis 不可用直接启动失败, 避免带病上线
    logger.info("auth-service started")
    yield
    await close_redis()
    await dispose_engine()


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(
        title=s.app_name,
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if s.app_env != "production" or s.debug else None,  # 生产默认关闭文档
        redoc_url=None,
    )

    build_middlewares(app)
    if s.cors_origin_list:
        app.add_middleware(CORSMiddleware, allow_origins=s.cors_origin_list, allow_credentials=False)

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
        return JSONResponse({"code": int(BizCode.BAD_REQUEST), "message": msg, "data": None}, status_code=400)

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled exception: %s %s", request.method, request.url.path)
        return JSONResponse(
            {"code": int(BizCode.INTERNAL), "message": "服务内部错误", "data": None}, status_code=500
        )

    @app.get("/health", tags=["infra"])
    async def health() -> dict:
        return {"status": "ok", "service": s.app_name, "env": s.app_env}

    app.include_router(api_router, prefix=s.api_prefix)
    return app


app = create_app()
