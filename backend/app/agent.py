"""Agent construction: chat model + LangChain agent + conversation memory.

会话记忆(LangGraph checkpointer)支持两种后端, 由 CHECKPOINT_BACKEND 切换:

- ``sqlite``(默认): 零外部依赖, 本地开发开箱即用;
- ``postgres``: 连接 ``CHECKPOINT_DATABASE_URL``, 首次启动自动建
  ``checkpoint_*`` 表(LangGraph 官方 checkpointer, 与 LangGraph Platform
  同源)。SQLite 文件无法跨容器共享, 生产/多副本部署应使用 Postgres。
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite
from langchain.agents import create_agent
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from app.config import Settings, get_settings
from app.tools import ALL_TOOLS

logger = logging.getLogger("agent")

Checkpointer = AsyncSqliteSaver | AsyncPostgresSaver


# ---------------------------------------------------------------------------
# Checkpointer lifecycle
#
# Both backends keep a single long-lived connection owned by the module and
# closed on shutdown so the event loop can exit cleanly.
# ---------------------------------------------------------------------------
_checkpointer: Checkpointer | None = None
_checkpointer_conn: AsyncConnection | aiosqlite.Connection | None = None
_checkpointer_lock = asyncio.Lock()


async def init_checkpointer() -> Checkpointer:
    """Open (or reuse) the configured checkpointer; creates tables on first use."""
    global _checkpointer, _checkpointer_conn
    if _checkpointer is None:
        async with _checkpointer_lock:
            if _checkpointer is None:
                settings = get_settings()
                if settings.checkpoint_backend == "postgres":
                    if not settings.checkpoint_database_url:
                        raise RuntimeError(
                            "CHECKPOINT_BACKEND=postgres 但未配置 CHECKPOINT_DATABASE_URL, "
                            "例如 postgres://langgraph:CHANGE_ME@127.0.0.1:5432/langgraph"
                        )
                    conn = await AsyncConnection.connect(
                        settings.checkpoint_database_url,
                        autocommit=True,
                        prepare_threshold=0,
                        row_factory=dict_row,
                    )
                    saver: Checkpointer = AsyncPostgresSaver(conn)
                    await saver.setup()
                    _checkpointer_conn = conn
                    _checkpointer = saver
                    logger.info("Agent checkpointer initialized (postgres)")
                else:
                    db_path = Path(settings.checkpoint_db_path)
                    db_path.parent.mkdir(parents=True, exist_ok=True)
                    conn = await aiosqlite.connect(db_path)
                    saver = AsyncSqliteSaver(conn)
                    await saver.setup()
                    _checkpointer_conn = conn
                    _checkpointer = saver
                    logger.info("Agent checkpointer initialized (sqlite path=%s)", db_path)
    return _checkpointer


async def close_checkpointer() -> None:
    """Close the underlying checkpointer connection if it was opened."""
    global _checkpointer, _checkpointer_conn
    saver, conn = _checkpointer, _checkpointer_conn
    _checkpointer, _checkpointer_conn = None, None
    if saver is not None and conn is not None:
        await conn.close()
        logger.info("Agent checkpointer closed")


# ---------------------------------------------------------------------------
# Mock model — used when no API key is configured, so the app runs out of the
# box for UI development. Supports plain replies AND one demo tool call so the
# full agent loop (model -> tool -> model) is visible in the UI.
# ---------------------------------------------------------------------------
_MOCK_REPLIES = [
    "你好！我是运行在 mock 模式下的演示助理。\n\n"
    "当前没有配置真实的 LLM API Key，所以只能返回这些固定回复。"
    "在 backend/.env 中配置 OPENAI_API_KEY（或 ANTHROPIC_API_KEY）后即可体验完整能力。\n\n"
    "不过你仍然可以试试：\n"
    "- 「现在几点了」—— 演示工具调用\n"
    "- 「北京天气怎么样」—— 演示天气工具\n"
    "- 「计算 12 * (3 + 4)」—— 演示计算器工具",
    "这是一条 mock 回复：前端与后端的流式链路已经打通。配置真实模型后，这里将是大模型的回答。",
]


def _extract_tool_call(messages: list[BaseMessage]) -> AIMessage | None:
    last_human = next((m for m in reversed(messages) if m.type == "human"), None)
    if last_human is None:
        return None
    text = str(last_human.content)
    if any(k in text for k in ("时间", "几点", "日期")):
        return AIMessage(
            content="",
            tool_calls=[{"name": "get_current_time", "args": {}, "id": "mock_call_time", "type": "tool_call"}],
        )
    if "天气" in text:
        city = "北京"
        for candidate in ("北京", "上海", "深圳", "杭州"):
            if candidate in text:
                city = candidate
                break
        return AIMessage(
            content="",
            tool_calls=[{"name": "get_weather", "args": {"city": city}, "id": "mock_call_weather", "type": "tool_call"}],
        )
    if any(ch.isdigit() for ch in text) and any(op in text for op in "+-*/×÷"):
        expression = text.translate(str.maketrans({"×": "*", "÷": "/", "：": ":"}))
        digits = "".join(ch for ch in expression if ch in "0123456789+-*/().% ")
        if digits.strip():
            return AIMessage(
                content="",
                tool_calls=[
                    {"name": "calculate", "args": {"expression": digits.strip()}, "id": "mock_call_calc", "type": "tool_call"}
                ],
            )
    return None


class MockChatModel(BaseChatModel):
    """A deterministic fake chat model that streams canned replies.

    It also emits a single tool call for time / weather / math questions so
    the complete agent loop can be exercised without any API key.
    """

    reply_index: int = 0

    @property
    def _llm_type(self) -> str:
        return "mock-chat-model"

    def bind_tools(self, tools, **kwargs):  # type: ignore[override]
        # The mock model decides on its own when to emit tool calls, so the
        # tool schemas are ignored. Returning self keeps the agent happy.
        return self

    def _next_content(self, messages: list[BaseMessage]) -> str:
        last = messages[-1]
        if last.type == "tool":
            return f"工具 `{last.name}` 的执行结果：{last.content}\n\n（这是 mock 模式的演示回答，配置真实 LLM 后回答会更自然。）"
        tool_call = _extract_tool_call(messages)
        if tool_call is None:
            reply = _MOCK_REPLIES[self.reply_index % len(_MOCK_REPLIES)]
            self.reply_index += 1
            return reply
        return ""

    def _next_message(self, messages: list[BaseMessage]) -> AIMessage:
        last = messages[-1]
        if last.type != "tool":
            tool_call = _extract_tool_call(messages)
            if tool_call is not None:
                return tool_call
        content = self._next_content(messages)
        return AIMessage(content=content)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._next_message(messages))])

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        message = self._next_message(messages)
        if message.tool_calls:
            chunk_message = AIMessageChunk(
                content="",
                tool_call_chunks=[
                    {
                        "name": tc["name"],
                        "args": json.dumps(tc["args"], ensure_ascii=False),
                        "id": tc["id"],
                        "index": i,
                    }
                    for i, tc in enumerate(message.tool_calls)
                ],
            )
            if run_manager:
                await run_manager.on_llm_new_token("", chunk=chunk_message)
            yield ChatGenerationChunk(message=chunk_message)
            return
        content = str(message.content)
        for start in range(0, len(content), 2):
            piece = content[start : start + 2]
            chunk_message = AIMessageChunk(content=piece)
            if run_manager:
                await run_manager.on_llm_new_token(piece, chunk=chunk_message)
            yield ChatGenerationChunk(message=chunk_message)
            await asyncio.sleep(0.02)


# ---------------------------------------------------------------------------
# Real model providers
# ---------------------------------------------------------------------------
def build_chat_model(settings: Settings) -> BaseChatModel:
    provider = settings.llm_provider.lower()

    if provider == "anthropic":
        if not settings.anthropic_api_key:
            raise RuntimeError("LLM_PROVIDER=anthropic 但未配置 ANTHROPIC_API_KEY")
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=settings.llm_model,
            api_key=settings.anthropic_api_key,
            timeout=settings.llm_timeout_seconds,
        )

    if provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("LLM_PROVIDER=openai 但未配置 OPENAI_API_KEY")
        from langchain_openai import ChatOpenAI

        kwargs = {
            "model": settings.llm_model,
            "api_key": settings.openai_api_key,
            "timeout": settings.llm_timeout_seconds,  # 上游挂死时快速失败, 由聊天路由兜底提示
        }
        if settings.openai_base_url:
            kwargs["base_url"] = settings.openai_base_url
        return ChatOpenAI(**kwargs)

    if provider == "mock":
        return MockChatModel()

    raise RuntimeError(f"未知的 LLM_PROVIDER: {provider}")


def _resolve_model(settings: Settings) -> BaseChatModel:
    """Build the configured model, falling back to the mock model when no key exists."""
    provider = settings.llm_provider.lower()
    has_key = (provider == "openai" and settings.openai_api_key) or (
        provider == "anthropic" and settings.anthropic_api_key
    )
    if provider in ("openai", "anthropic") and not has_key:
        logger.warning(
            "未检测到 %s API Key，自动降级为 mock 模型（仅用于演示）。"
            "请在 backend/.env 中配置后重启。",
            provider.upper(),
        )
        return MockChatModel()
    return build_chat_model(settings)


_agent: Any | None = None
_agent_lock = asyncio.Lock()


async def get_agent() -> Any:
    """Create the agent once: model + tools + configured conversation store."""
    global _agent
    if _agent is None:
        async with _agent_lock:
            if _agent is None:
                settings = get_settings()
                model = _resolve_model(settings)
                checkpointer = await init_checkpointer()
                _agent = create_agent(
                    model=model,
                    tools=ALL_TOOLS,
                    system_prompt=settings.system_prompt,
                    checkpointer=checkpointer,
                )
                logger.info(
                    "Agent ready (provider=%s, model=%s)", settings.llm_provider, settings.llm_model
                )
    return _agent


if __name__ == "__main__":
    async def _demo() -> None:
        agent = await get_agent()
        user_message = {"role": "user", "content": "你是谁"}
        config = {"configurable": {"thread_id": "123"}}
        ret = await agent.ainvoke(input=user_message, config=config)
        print(ret)
        await close_checkpointer()

    asyncio.run(_demo())
