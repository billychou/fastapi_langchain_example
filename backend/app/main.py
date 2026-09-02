"""FastAPI entrypoint: SSE streaming chat + 用户认证/RBAC 一体化服务。

- /api/health       健康检查(匿名)
- /api/chat         SSE 聊天(默认要求登录, CHAT_REQUIRE_AUTH 控制)
- /api/v1/auth/*    注册/登录/刷新/登出/改密/OAuth
- /api/v1/account/* 当前用户资料与在线会话
- /api/v1/admin/*   管理端(RBAC 权限点控制)
"""

import json
import logging
import uuid
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

from app.agent import close_checkpointer, get_agent, init_checkpointer
from app.api.v1.router import api_router
from app.config import get_settings
from app.core.net import resolve_client_ip
from app.core.ratelimit import enforce_chat_rate
from app.db.redis import close_redis, get_redis_client, init_redis
from app.db.session import dispose_engine, get_db, get_engine
from app.deps import AuthContext, get_current_optional
from app.exceptions import BizCode, BizError
from app.middleware.auth import build_middlewares
from app.schemas import ChatRequest, MessageType
from app.schemas.chat import DEFAULT_CONVERSATION_ID
from app.services import thread_service
from sqlalchemy.ext.asyncio import AsyncSession

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
logger = logging.getLogger("api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """预热资源: 数据库引擎惰性创建; Redis 探活失败仅告警(聊天功能可降级运行)。"""
    get_engine()
    await init_checkpointer()  # 打开 SQLite checkpointer 连接并建表
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

    # ---------------- 聊天(SSE) ----------------
    @app.post("/api/chat")
    async def chat(
        request: ChatRequest,
        raw_request: Request,
        ctx: AuthContext | None = Depends(get_current_optional),
        db: AsyncSession = Depends(get_db),
    ) -> StreamingResponse:
        # 限流: 登录按账号/匿名按 IP; Redis 不可用时跳过并告警(演示降级模式)
        redis = get_redis_client()
        if redis is not None:
            await enforce_chat_rate(
                redis,
                account_id=ctx.account_id if ctx is not None else None,
                ip=resolve_client_ip(raw_request),
            )
        else:
            logger.warning("Redis 不可用, 聊天限流未生效")

        thread_id = request.conversation_id
        if ctx is not None:
            # 会话元数据行不存在则自动创建(首次聊天即建档); 他人会话 → 404
            await thread_service.ensure_thread(db, ctx.account_id, thread_id)
        elif thread_id == DEFAULT_CONVERSATION_ID:
            # 匿名 + 默认会话号: 每请求随机生成, 防止陌生人共享同一 checkpointer
            # 记忆; 显式传 conversation_id 视为有意的共享会话。
            thread_id = uuid.uuid4().hex
        return StreamingResponse(
            chat_events(request, ctx, db, thread_id, raw_request),
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


async def chat_events(
    request: ChatRequest,
    ctx: AuthContext | None,
    db: AsyncSession,
    thread_id: str,
    raw_request: Request,
) -> AsyncIterator[str]:
    """Stream the agent's reply as OpenAI-style SSE chunks.

    Each `data:` line is a JSON object of shape
    `{"choices": [{"delta": {"content": "..."}}]}`, matching what
    `@ant-design/x-sdk`'s `DeepSeekChatProvider` parses. Tool calls and
    tool results from the agent are not surfaced — the agent will emit a
    final text message after any tool loop, which is what the UI renders.

    The stream is terminated with `data: [DONE]`. If the agent raises,
    a sanitized notice (with request-id for tracing) is appended as a final
    content delta; the raw exception only goes to server logs.

    登录态下, 一轮成功结束后更新 agent_threads 元数据(标题/预览/时间);
    元数据写入失败不影响 SSE 输出。

    thread_id 由路由层决定: 登录用会话 ID; 匿名未显式指定时为每请求随机值。
    """
    agent = await get_agent()
    config = {"configurable": {"thread_id": thread_id}}

    first_user_text = next(
        (m.content for m in reversed(request.messages) if m.role == "user"), ""
    )
    assistant_parts: list[str] = []

    try:
        async for chunk, _metadata in agent.astream(
            {"messages": _to_langchain_messages(request.messages)},
            config=config,
            stream_mode="messages",
        ):
            if isinstance(chunk, AIMessageChunk):
                text = _chunk_text(chunk.content)
                if text:
                    assistant_parts.append(text)
                    yield sse({"choices": [{"delta": {"content": text}}]})
    except Exception:  # noqa: BLE001 - 完整错误仅落服务端日志, 客户端只收脱敏消息
        logger.exception("Agent stream failed")
        request_id = getattr(raw_request.state, "request_id", "-")
        yield sse(
            {
                "choices": [
                    {"delta": {"content": f"\n\n[服务暂时不可用, 请稍后重试 (request_id: {request_id})]"}}
                ]
            }
        )
    else:
        if ctx is not None:
            await thread_service.record_exchange(
                db, ctx.account_id, thread_id, first_user_text, "".join(assistant_parts)
            )

    yield "data: [DONE]\n\n"


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=5001)
