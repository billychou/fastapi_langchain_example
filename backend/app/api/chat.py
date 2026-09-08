"""/api/chat SSE 聊天路由。

路径保持 /api/chat(不带 /v1): 前端与 OpenAI 风格 SSE 协议均按此约定对接。
登录态由 get_current_optional 控制(CHAT_REQUIRE_AUTH), 限流/匿名隔离策略见路由实现。
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import get_agent
from app.context import ChatContext
from app.core.net import resolve_client_ip
from app.core.ratelimit import enforce_chat_rate
from app.db.redis import get_redis_client
from app.db.session import get_db
from app.deps import AuthContext, get_current_optional
from app.schemas import ChatRequest, MessageType
from app.schemas.chat import DEFAULT_CONVERSATION_ID
from app.services import thread_service

logger = logging.getLogger("api.chat")

router = APIRouter()


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


# 工具结果回传前端时的最大字符数: 结果仅用于思维链展示, 超长截断防止 SSE 膨胀
_TOOL_RESULT_MAX_CHARS = 2000


def _tool_events(message: BaseMessage) -> list[dict[str, Any]]:
    """Extract UI-facing tool-chain events from one completed agent message.

    - ``AIMessage`` with ``tool_calls`` → one ``tool_call`` event per call;
    - ``ToolMessage`` → one ``tool_result`` event (content truncated).

    Returns an empty list for plain text messages, so callers can yield
    unconditionally without filtering.
    """
    events: list[dict[str, Any]] = []
    if isinstance(message, AIMessage):
        for call in message.tool_calls or []:
            events.append(
                {
                    "type": "tool_call",
                    "id": call.get("id") or "",
                    "name": call.get("name") or "",
                    "args": call.get("args") or {},
                }
            )
    elif isinstance(message, ToolMessage):
        result = _chunk_text(message.content)
        if len(result) > _TOOL_RESULT_MAX_CHARS:
            result = result[:_TOOL_RESULT_MAX_CHARS] + "…"
        events.append(
            {
                "type": "tool_result",
                "id": message.tool_call_id or "",
                "name": message.name or "",
                "result": result,
            }
        )
    return events


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


@router.post("/chat")
async def chat(
    request: ChatRequest,
    raw_request: Request,
    ctx: AuthContext | None = Depends(get_current_optional),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    # 限流: 登录按账号/匿名按 IP; Redis 不可用时跳过并告警(演示降级模式)
    redis = get_redis_client()
    if redis is not None:
        try:
            await enforce_chat_rate(
                redis,
                account_id=ctx.account_id if ctx is not None else None,
                ip=resolve_client_ip(raw_request),
            )
        except RedisError as exc:
            # Redis 启动后宕机: 限流降级放行, 保证聊天可用性(与 lifespan 降级策略一致)
            logger.warning("Redis 不可用, 聊天限流未生效: %s", exc)
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
    `@ant-design/x-sdk`'s `DeepSeekChatProvider` parses (it only reads
    `choices[].delta.content/reasoning_content`, so extra fields are
    backward compatible).

    Tool-chain visibility: besides text deltas, the stream carries agent
    events on the top-level `agent` field —
    `{"choices": [{"delta": {}}], "agent": {"type": "tool_call"|"tool_result",
    "id": ..., "name": ..., "args"|"result": ...}}`. `tool_call` is emitted
    when the model node finishes a message that requests tools; `tool_result`
    when the tools node finishes (result text truncated). UIs can render a
    thought chain from them; older clients simply ignore the field.

    The stream is terminated with `data: [DONE]`. If the agent raises,
    a sanitized notice (with request-id for tracing) is appended as a final
    content delta; the raw exception only goes to server logs.

    登录态下, 一轮成功结束后更新 agent_threads 元数据(标题/预览/时间);
    元数据写入失败不影响 SSE 输出。

    thread_id 由路由层决定: 登录用会话 ID; 匿名未显式指定时为每请求随机值。

    技能(skills): 调用者身份经 ChatContext 传入 agent, SkillMiddleware 据此把可见技能
    目录写进系统提示, 并对无权限的技能调用做服务端拦截。
    """
    agent = await get_agent()
    config = {"configurable": {"thread_id": thread_id}}
    # agent 是进程级单例, 身份/权限只能按请求经 context 传入(技能可见性据此过滤)
    context = ChatContext(
        account_id=ctx.account_id if ctx is not None else 0,
        authenticated=ctx is not None,
        permissions=frozenset(ctx.permissions) if ctx is not None else frozenset(),
    )

    first_user_text = next((m.content for m in reversed(request.messages) if m.role == "user"), "")
    assistant_parts: list[str] = []

    try:
        # messages 模式输出 LLM token 增量 (chunk, metadata);
        # updates 模式输出节点级完整消息, 用于提取工具调用/结果事件。
        async for mode, chunk in agent.astream(
            {"messages": _to_langchain_messages(request.messages)},
            config=config,
            context=context,
            stream_mode=["messages", "updates"],
        ):
            if mode == "messages":
                message_chunk, _metadata = chunk
                if isinstance(message_chunk, AIMessageChunk):
                    text = _chunk_text(message_chunk.content)
                    if text:
                        assistant_parts.append(text)
                        yield sse({"choices": [{"delta": {"content": text}}]})
            else:  # mode == "updates": {node: {"messages": [...]}}
                for node_output in chunk.values():
                    for message in (node_output or {}).get("messages", []):
                        for event in _tool_events(message):
                            yield sse({"choices": [{"delta": {}}], "agent": event})
    except Exception:  # noqa: BLE001 - 完整错误仅落服务端日志, 客户端只收脱敏消息
        logger.exception("Agent stream failed")
        request_id = getattr(raw_request.state, "request_id", "-")
        yield sse(
            {
                "choices": [
                    {
                        "delta": {
                            "content": f"\n\n[服务暂时不可用, 请稍后重试 (request_id: {request_id})]"
                        }
                    }
                ]
            }
        )
    else:
        if ctx is not None:
            await thread_service.record_exchange(
                db, ctx.account_id, thread_id, first_user_text, "".join(assistant_parts)
            )

    yield "data: [DONE]\n\n"
