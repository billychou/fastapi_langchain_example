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
from app.config import get_settings
from app.context import ChatContext
from app.core.messages import message_text
from app.core.net import resolve_client_ip
from app.core.ratelimit import enforce_chat_rate
from app.db.redis import get_redis_client
from app.db.session import get_db
from app.deps import AuthContext, get_current_optional
from app.middleware.agent_pedagogy import STATE_CHANNEL
from app.pedagogy.events import LearnEvent
from app.pedagogy.session import PedagogyTurn, prepare_turn
from app.schemas import ChatRequest, MessageType
from app.schemas.chat import DEFAULT_CONVERSATION_ID
from app.services import thread_service

logger = logging.getLogger("api.chat")

router = APIRouter()


def sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def learn_sse(event: LearnEvent) -> str:
    """一条 ``learn`` 事件(学习证据面板的数据源)。

    与既有 ``agent`` 事件同构: 顶层平铺一个字段 + 一个空的 ``choices[].delta``,
    因此只读 ``delta.content`` 的老客户端(包括 ``@ant-design/x-sdk`` 的默认 provider)
    会**自动忽略**它, 不需要版本协商。
    """
    return sse({"choices": [{"delta": {}}], "learn": event.to_sse()})


def _chunk_text(content: Any) -> str:
    """Normalize message content (str or list of content blocks) to text.

    实现在 :mod:`app.core.messages`(教学中间件也要按同一口径数字数), 这里保留别名
    以免改动本文件里三处调用点。
    """
    return message_text(content)


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

    学习证据(learn): 登录态 + `PEDAGOGY_ENABLED` 打开 + 本轮是学习会话时, 流里会多一个
    顶层 `learn` 字段 —— `{"choices": [{"delta": {}}], "learn": {"type": "state"|"gap"|
    "guard"|"attachment"|"note", "state": ..., "loop_index": ..., "data": {...}}}`。
    `state`/`gap` 在调模型**之前**就下发(路由侧判读学习者发言即可确定), `guard`/
    `attachment` 在模型说完之后下发。护栏改写发生在流式输出之后, 所以 `guard.data.
    replacement` 携带改写后的**全文**, 由前端替换气泡内容; 只读 `delta` 的客户端
    不受任何影响。
    """
    agent = await get_agent()
    config = {"configurable": {"thread_id": thread_id}}
    first_user_text = next((m.content for m in reversed(request.messages) if m.role == "user"), "")

    # 教学回合装配: 读 checkpointer 的 pedagogy 频道 -> 判读学习者这句话 -> tick。
    # 返回 None 就是普通聊天(闲聊/匿名/降级开关关闭), 下面的行为与注入前逐字一致。
    turn = await _prepare_pedagogy(agent, ctx, thread_id, first_user_text, config)

    # agent 是进程级单例, 身份/权限/教学状态只能按请求经 context 传入
    identity: dict[str, Any] = {
        "account_id": ctx.account_id if ctx is not None else 0,
        "authenticated": ctx is not None,
        "permissions": frozenset(ctx.permissions) if ctx is not None else frozenset(),
    }
    context = ChatContext(**identity, **(turn.context_kwargs if turn else {}))
    sink = turn.sink if turn is not None else None

    assistant_parts: list[str] = []
    # 护栏改写过的消息会被 after_model 以「同 id 的新消息」再报一次(updates lane 的
    # 节点名变成 PedagogyMiddleware.after_model), 不去重前端就会看到两次工具调用。
    seen_events: set[str] = set()

    if turn is not None:
        # 调模型之前就能确定的事件先走: 学习者应该先看到「现在在哪一步」,
        # 再看到模型的回复, 而不是等流结束才补一张状态卡。
        for event in turn.pre_events:
            yield learn_sse(event)

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
                            key = f"{event['type']}:{event['id']}:{event['name']}"
                            if key in seen_events:
                                continue
                            seen_events.add(key)
                            yield sse({"choices": [{"delta": {}}], "agent": event})
            # 中间件在模型说完之后才产出护栏/诊断事件; 每次循环都拉一把,
            # 它们才能与 token 增量交错下发, 而不是堆在流末尾。
            if sink is not None:
                for event in sink.drain():
                    yield learn_sse(event)
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
            # 这里记的是**流式输出过的原文**: 护栏改写发生在 token 已经出门之后,
            # 服务端收不回来。checkpointer 里存的是改写后的版本, 前端按
            # learn.guard.data.replacement 替换气泡(#9)。会话标题/预览只是索引,
            # 学习证据的权威副本在 learner_explanation / mastery_ledger(#7)。
            await thread_service.record_exchange(
                db, ctx.account_id, thread_id, first_user_text, "".join(assistant_parts)
            )

    if sink is not None:
        for event in sink.drain():
            yield learn_sse(event)

    yield "data: [DONE]\n\n"


async def _prepare_pedagogy(
    agent: Any,
    ctx: AuthContext | None,
    thread_id: str,
    text: str,
    config: dict[str, Any],
) -> PedagogyTurn | None:
    """装配本轮教学回合; 任何一步失败都退回普通聊天(``None``)。

    跨轮载体存在 LangGraph checkpointer 的 ``pedagogy`` 频道里(architecture §5 的 L1
    记忆), 所以这里要 ``aget_state`` 一次。**不新增任何存储**, 也天然按 ``thread_id``
    隔离; 未知线程返回空快照而不抛异常(LangGraph 的行为)。

    读频道失败一律降级为「本轮不教学」而不是 500: 状态机是可重建的辅助信息,
    账本在 MySQL, 教学增强不该把聊天本身拖下水。
    """
    if ctx is None or not get_settings().pedagogy_enabled:
        return None
    try:
        snapshot = await agent.aget_state(config)
        raw_channel = (snapshot.values or {}).get(STATE_CHANNEL)
    except Exception:  # noqa: BLE001 - checkpointer 不可用时聊天仍要可用
        logger.warning("读取 pedagogy 频道失败, 本轮按普通聊天处理", exc_info=True)
        return None
    try:
        return prepare_turn(
            raw_channel=raw_channel,
            thread_id=thread_id,
            account_id=ctx.account_id,
            text=text,
        )
    except Exception:  # noqa: BLE001 - 同上: 教学装配失败不得影响聊天
        logger.warning("装配教学回合失败, 本轮按普通聊天处理", exc_info=True)
        return None
