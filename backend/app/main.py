"""FastAPI entrypoint: SSE streaming chat + 用户认证/RBAC 一体化服务。

- /api/health       健康检查(匿名)
- /api/chat         SSE 聊天(默认要求登录, CHAT_REQUIRE_AUTH 控制)
- /api/v1/auth/*    注册/登录/刷新/登出/改密/OAuth
- /api/v1/account/* 当前用户资料与在线会话
- /api/v1/admin/*   管理端(RBAC 权限点控制)
"""

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)

from app.agent import get_agent
from app.api.v1.router import api_router
from app.config import get_settings
from app.db.redis import close_redis, init_redis
from app.db.session import dispose_engine, get_engine
from app.deps import AuthContext, get_current
from app.exceptions import BizCode, BizError
from app.middleware.auth import build_middlewares
from app.schemas import ChatRequest, MessageType

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
logger = logging.getLogger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """预热资源: 数据库引擎惰性创建; Redis 探活失败仅告警(聊天功能可降级运行)。"""
    get_engine()
    try:
        redis = init_redis()
        await redis.ping()
    except Exception as exc:  # noqa: BLE001 - Redis 不可用时聊天仍可降级服务
        logger.warning("Redis 不可用, 认证相关接口将无法工作: %s", exc)
    logger.info("backend started")
    yield
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

    # ---------------- 聊天(SSE) ----------------
    chat_dependencies = [Depends(get_current)] if settings.chat_require_auth else []

    @app.post("/api/chat", dependencies=chat_dependencies)
    async def chat(request: ChatRequest) -> StreamingResponse:
        return StreamingResponse(
            chat_events(request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    app.include_router(api_router, prefix=settings.api_prefix)
    return app


def sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _chunk_text(content: Any) -> str:
    """Normalize message content (str or list of content blocks) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return ""


def _to_langchain_messages(messages: list[MessageType]) -> list[BaseMessage]:
    """Map OpenAI-style request messages to LangChain messages by `role`.

    Falls back to HumanMessage for unknown roles so the agent always receives
    a non-empty history.
    """
    result: list[BaseMessage] = []
    for m in messages:
        content = m.content
        if m.role == "user":
            result.append(HumanMessage(content=content))
        elif m.role == "assistant":
            result.append(AIMessage(content=content))
        elif m.role == "system":
            result.append(SystemMessage(content=content))
        else:
            result.append(HumanMessage(content=content))
    return result


async def chat_events(request: ChatRequest) -> AsyncIterator[str]:
    """Stream the agent's reply as OpenAI-style SSE chunks.

    Each `data:` line is a JSON object of shape
    `{"choices": [{"delta": {"content": "..."}}]}`, matching what
    `@ant-design/x-sdk`'s `DeepSeekChatProvider` parses. Tool calls and
    tool results from the agent are not surfaced — the agent will emit a
    final text message after any tool loop, which is what the UI renders.

    The stream is terminated with `data: [DONE]`. If the agent raises,
    the error message is appended as a final content delta so it surfaces
    in the chat bubble instead of being silently dropped.
    """
    agent = get_agent()
    config = {"configurable": {"thread_id": request.conversation_id}}

    try:
        async for chunk, _metadata in agent.astream(
            {"messages": _to_langchain_messages(request.messages)},
            config=config,
            stream_mode="messages",
        ):
            if isinstance(chunk, AIMessageChunk):
                text = _chunk_text(chunk.content)
                if text:
                    yield sse({"choices": [{"delta": {"content": text}}]})
    except Exception as exc:  # noqa: BLE001 - surface any agent error to the client
        logger.exception("Agent stream failed")
        yield sse({"choices": [{"delta": {"content": f"\n\n[Agent 调用失败: {exc}]"}}]})

    yield "data: [DONE]\n\n"


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=5001)
