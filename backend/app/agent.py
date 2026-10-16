"""Agent construction: chat model + LangGraph multi-agent runtime + conversation memory.

多智能体运行时(supervisor -> executor, handoff-as-tools 模式):

- 注册表(``agent`` 表, 见 ``app/agent_registry.py``)是唯一的构造配方来源: 每个
  **active** 执行智能体编译成一个独立子图(自己的工具集/系统提示/技能白名单);
  总管子图额外获得 ``call_<agent_key>(task)`` handoff 工具, 模型调用它即把任务
  交给对应子图, 工具返回子图的最终文本;
- 带状态子图(教学智能体 ``edu-agent``)挂 ``PedagogyMiddleware`` 并复用主
  checkpointer, 线程 ID 派生为 ``f"{thread_id}::{agent_key}"``, 费曼循环因此跨轮
  存活; 其余执行智能体每次调用无状态;
- 调度过程经 ``ChatContext.agent_sink`` 产出 ``agent_dispatch`` / ``agent_return``
  事件, 由 /api/chat drain 成 SSE(见 ``app/agent_events.py``)。

会话记忆(LangGraph checkpointer)支持两种后端, 由 CHECKPOINT_BACKEND 切换:

- ``sqlite``(默认): 零外部依赖, 本地开发开箱即用;
- ``postgres``: 连接 ``CHECKPOINT_DATABASE_URL``, 首次启动自动建
  ``checkpoint_*`` 表(LangGraph 官方 checkpointer, 与 LangGraph Platform
  同源)。SQLite 文件无法跨容器共享, 生产/多副本部署应使用 Postgres。

同一个 checkpointer 还顺带承载**教学状态**: ``PedagogyMiddleware`` 通过 ``state_schema``
给图状态加了一个 ``pedagogy`` 频道(值是 ``LoopCarrier`` 的 JSON), 费曼循环因此跨轮
存活, 且不需要任何新表/新键/新 TTL(见 ``docs/education-agent-architecture.md`` §5)。
多智能体化之后该频道挂在 **edu-agent 子图**上(派生线程), 主图不再挂教学中间件。
"""

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite
from langchain.agents import create_agent
from langchain.tools import ToolRuntime, tool
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from pydantic import Field

from app.agent_events import AgentEvent
from app.agent_registry import AgentSpec, load_agent_specs
from app.config import Settings, get_settings
from app.context import ChatContext
from app.core.messages import message_text
from app.middleware.agent_dispatch import DispatchMiddleware
from app.middleware.agent_pedagogy import PedagogyMiddleware
from app.middleware.agent_skills import SkillMiddleware
from app.skills.tools import SKILL_TOOLS
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
    """Close the underlying checkpointer connection if it was opened.

    同时丢弃缓存的运行时: 每个图构造时就绑定了这个 checkpointer, 连接关掉后它们已不可用;
    而且 AsyncSqliteSaver/AsyncPostgresSaver 内部的 asyncio 原语会绑定首次使用它们的事件循环,
    进程重启(或测试里换一个事件循环)后继续复用会抛
    "RuntimeError: ... is bound to a different event loop"。下次 get_runtime() 会重建。
    """
    global _checkpointer, _checkpointer_conn, _runtime
    global _runtime_lock, _runtime_lock_loop
    saver, conn = _checkpointer, _checkpointer_conn
    _checkpointer, _checkpointer_conn = None, None
    _runtime = None
    _runtime_lock, _runtime_lock_loop = None, None
    if saver is not None and conn is not None:
        await conn.close()
        logger.info("Agent checkpointer closed")


# ---------------------------------------------------------------------------
# Mock model — used when no API key is configured, so the app runs out of the
# box for UI development. Supports plain replies AND one demo tool call so the
# full agent loop (model -> tool -> model) is visible in the UI.
# ---------------------------------------------------------------------------
#: handoff 工具名前缀: 总管调用 ``call_<agent_key>`` 即把任务交给该执行智能体。
HANDOFF_TOOL_PREFIX = "call_"
#: 带状态(教学循环)的执行智能体: 子图挂 PedagogyMiddleware + 主 checkpointer。
LEARNING_AGENT_KEY = "edu-agent"
#: DispatchMiddleware 写进系统提示的机器标记, mock 模型据此确定性地复现调度。
DISPATCH_MARKER_RE = re.compile(r"\[dispatch:([a-zA-Z0-9_-]+)\]")
#: 总管系统提示的流式约定: 执行智能体全文已实时展示, 最终回复只做汇总。
SUPERVISOR_STREAM_NOTE = (
    "\n\n约定: 执行智能体的完整回答会实时流式展示给用户; 你的最终回复只做简短的"
    "跨智能体汇总、结论与下一步建议, 不要复述执行智能体的全文。"
)

_MOCK_REPLIES = [
    "你好！我是运行在 mock 模式下的演示助理。\n\n"
    "当前没有配置真实的 LLM API Key，所以只能返回这些固定回复。"
    "在 backend/.env 中配置 OPENAI_API_KEY（或 ANTHROPIC_API_KEY）后即可体验完整能力。\n\n"
    "不过你仍然可以试试：\n"
    "- 「现在几点了」—— 演示工具调用\n"
    "- 「北京天气怎么样」—— 演示天气工具\n"
    "- 「计算 12 * (3 + 4)」—— 演示计算器工具\n"
    "- 「你有什么技能」—— 演示技能(skills)加载\n"
    "- 「集群里有哪些 pod」—— 演示总管调度执行智能体",
    "这是一条 mock 回复：前端与后端的流式链路已经打通。配置真实模型后，这里将是大模型的回答。",
]


def _demo_skill_name(messages: list[BaseMessage]) -> str | None:
    """取技能目录里第一个名字, 用于 mock 模式演示技能加载链路。

    优先读系统提示里 SkillMiddleware 渲染的目录(多智能体下每个智能体只看到自己
    绑定的技能), 目录缺失时回落到匿名可见的第一个公开技能。
    """
    for message in messages:
        if message.type != "system":
            continue
        for line in str(message.content).splitlines():
            match = re.match(r"- ([a-z0-9][a-z0-9_-]*) \(v", line)
            if match:
                return match.group(1)
    from app.skills import get_skill_registry

    try:
        registry = get_skill_registry()
    except Exception:  # noqa: BLE001 - 技能子系统异常不应影响 mock 聊天
        return None
    public = [s.name for s in registry.all() if s.visibility == "public"]
    return public[0] if public else None


def _mock_args(name: str, text: str) -> dict[str, Any]:
    """mock 直连工具的演示参数: 只填必填项, 让工具真的跑一遍。"""
    if name == "get_weather":
        city = "北京"
        for candidate in ("北京", "上海", "深圳", "杭州"):
            if candidate in text:
                city = candidate
                break
        return {"city": city}
    if name == "calculate":
        expression = text.translate(str.maketrans({"×": "*", "÷": "/", "：": ":"}))
        return {"expression": "".join(ch for ch in expression if ch in "0123456789+-*/().% ").strip()}
    if name == "k8s_list_resources":
        return {"kind": "pod", "namespace": "default"}
    if name == "prometheus_query":
        return {"promql": "up", "minutes": 15}
    if name == "loki_query":
        return {"logql": '{job="app"} |= "error"', "limit": 20}
    return {}


#: mock 关键词路由: (关键词, 候选工具名)。候选按「直连工具优先, handoff 兜底」排序,
#: 第一个**已绑定**的候选胜出 —— 于是同一句话在总管身上变成调度、在执行智能体身上
#: 变成直连工具调用, 与真实模型的分工一致。
_MOCK_ROUTES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("技能", "skill", "会做什么"), ("load_skill",)),
    (("几点", "时间", "日期"), ("get_current_time",)),
    (("天气",), ("get_weather",)),
    (
        ("pod", "集群", "节点", "node", "deployment", "副本", "k8s", "kubernetes", "重启"),
        ("k8s_list_resources", f"{HANDOFF_TOOL_PREFIX}k8s-agent"),
    ),
    (("日志", "loki", "堆栈", "报错"), ("loki_query", f"{HANDOFF_TOOL_PREFIX}log-agent")),
    (
        ("指标", "prometheus", "cpu", "内存", "延迟", "qps", "监控"),
        ("prometheus_query", f"{HANDOFF_TOOL_PREFIX}metric-agent"),
    ),
    (("知识库", "runbook", "复盘", "手册", "文档"), (f"{HANDOFF_TOOL_PREFIX}rag-agent",)),
    (
        ("想搞懂", "教我", "解释一下", "怎么理解", "什么是", "学习"),
        (f"{HANDOFF_TOOL_PREFIX}{LEARNING_AGENT_KEY}",),
    ),
)


def _extract_tool_call(messages: list[BaseMessage], bound: list[str] | None = None) -> AIMessage | None:
    """mock 模型的「决策」: 系统提示里的调度标记 > 关键词路由 > 算术兜底。

    ``bound`` 是 ``bind_tools`` 记下的本次请求工具名; 为空(老用法/子类未记录)时不做
    过滤, 保持注入多智能体之前的行为。
    """
    available = set(bound) if bound else None

    def pick(candidates: tuple[str, ...]) -> str | None:
        for name in candidates:
            if available is None or name in available:
                return name
        return None

    last_human = next((m for m in reversed(messages) if m.type == "human"), None)
    if last_human is None:
        return None
    text = str(last_human.content)

    # 1) DispatchMiddleware 的调度标记(教学回合必须落到教学智能体)
    for message in messages:
        if message.type != "system":
            continue
        match = DISPATCH_MARKER_RE.search(str(message.content))
        if match and (available is None or match.group(1) in available):
            name = match.group(1)
            return AIMessage(
                content="",
                tool_calls=[
                    {"name": name, "args": {"task": text}, "id": f"mock_call_{name}", "type": "tool_call"}
                ],
            )
        if match:
            break

    # 2) 关键词路由
    for keywords, candidates in _MOCK_ROUTES:
        if any(k in text for k in keywords):
            name = pick(candidates)
            if name is None:
                continue
            args = {"task": text} if name.startswith(HANDOFF_TOOL_PREFIX) else _mock_args(name, text)
            if name == "load_skill":
                skill_name = _demo_skill_name(messages)
                if skill_name is None:
                    continue
                args = {"name": skill_name}
            if name == "calculate" and not args.get("expression"):
                continue
            return AIMessage(
                content="",
                tool_calls=[{"name": name, "args": args, "id": f"mock_call_{name}", "type": "tool_call"}],
            )

    # 3) 算术兜底(保留注入前的行为: 带数字与运算符就走计算器)
    if any(ch.isdigit() for ch in text) and any(op in text for op in "+-*/×÷"):
        if available is None or "calculate" in available:
            args = _mock_args("calculate", text)
            if args.get("expression"):
                return AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "calculate", "args": args, "id": "mock_call_calc", "type": "tool_call"}
                    ],
                )
    return None


class MockChatModel(BaseChatModel):
    """A deterministic fake chat model that streams canned replies.

    It also emits tool calls for time / weather / math / ops questions and
    honours the supervisor's handoff tools, so the complete multi-agent loop
    (supervisor -> executor -> summary) can be exercised without any API key.
    """

    reply_index: int = 0
    #: ``bind_tools`` 记下的工具名: mock 只调用本次请求真正绑定的工具。
    bound_tools: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "mock-chat-model"

    def bind_tools(self, tools, **kwargs):  # type: ignore[override]
        # The mock model decides on its own when to emit tool calls, so the
        # tool schemas are ignored; the names are kept so keyword routing can
        # never invent a tool the graph did not register.
        self.bound_tools = [getattr(item, "name", "") for item in tools if getattr(item, "name", "")]
        return self

    def _next_content(self, messages: list[BaseMessage]) -> str:
        last = messages[-1]
        if last.type == "tool":
            if last.additional_kwargs.get("already_streamed"):
                # 全文已实时流给用户: mock 总管不再复述(真实模型由系统提示约束)。
                return ""
            result = str(last.content)
            if (last.name or "").startswith(HANDOFF_TOOL_PREFIX):
                # 执行智能体已经组织好最终回答, 总管原样转述(mock 不做二次加工,
                # 前端靠 agent_dispatch/agent_return 事件展示调度链)。
                return result
            return f"工具 `{last.name}` 的执行结果：{result}\n\n（这是 mock 模式的演示回答，配置真实 LLM 后回答会更自然。）"
        tool_call = _extract_tool_call(messages, self.bound_tools)
        if tool_call is None:
            reply = _MOCK_REPLIES[self.reply_index % len(_MOCK_REPLIES)]
            self.reply_index += 1
            return reply
        return ""

    def _next_message(self, messages: list[BaseMessage]) -> AIMessage:
        last = messages[-1]
        if last.type != "tool":
            tool_call = _extract_tool_call(messages, self.bound_tools)
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
                        "index": index,
                    }
                    for index, tc in enumerate(message.tool_calls)
                ],
            )
            yield ChatGenerationChunk(message=chunk_message)
            return
        content = str(message.content)
        if not content:
            # 空回复(执行智能体全文已实时流过, 总管不再复述): 也必须产出一个
            # 空 content 的 chunk, 否则 langchain_core 判「流里无 generation」直接
            # ValueError, 整轮聊天降级。
            yield ChatGenerationChunk(message=AIMessageChunk(content=""))
            return
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


# ---------------------------------------------------------------------------
# Multi-agent runtime: registry -> supervisor + executor subgraphs
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class AgentRuntime:
    """一次注册表快照编译出的全部图: 总管(聊天入口) + 各执行智能体子图。"""

    supervisor: Any
    supervisor_spec: AgentSpec
    executors: dict[str, Any]
    specs: tuple[AgentSpec, ...]
    handoff_tool_names: tuple[str, ...]

    def executor(self, agent_key: str) -> Any | None:
        return self.executors.get(agent_key)


def _resolve_tools(names: tuple[str, ...], *, owner: str) -> list[Any]:
    """把 ``tool_refs`` 解析成本地工具对象; 未知名字告警跳过(不炸启动)。"""
    known = {item.name: item for item in ALL_TOOLS}
    resolved: list[Any] = []
    for name in names:
        item = known.get(name)
        if item is None:
            logger.warning("智能体 %s 引用了不存在的本地工具: %s", owner, name)
            continue
        resolved.append(item)
    return resolved


def _final_text(result: Any) -> str:
    """子图结果里的最后一条非空 AI 文本 = 执行智能体的最终回答。"""
    messages = result.get("messages", []) if isinstance(result, dict) else []
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            text = message_text(message.content)
            if text:
                return text
    return ""


def _subgraph_config(ctx: ChatContext, agent_key: str, *, stateful: bool) -> dict[str, Any]:
    """子图调用配置: **切断父图回调**(否则子图 token 会混进主 SSE 流造成重复正文),
    带状态子图另给一条派生线程, 让教学循环跨轮存活。"""
    config: dict[str, Any] = {"callbacks": []}
    if stateful:
        base = ctx.thread_id or uuid.uuid4().hex
        config["configurable"] = {"thread_id": f"{base}::{agent_key}"}
    return config


def _build_handoff_tool(spec: AgentSpec, graph: Any, *, stateful: bool) -> Any:  # noqa: C901
    """``call_<agent_key>(task)``: 跑执行智能体子图并返回其最终文本。

    调度前后各发一条事件(agent_dispatch / agent_return), 经 ``ChatContext.agent_sink``
    交给 SSE 路由; 子图异常只降级为一句提示, 不让总管整轮失败。
    """
    name = f"{HANDOFF_TOOL_PREFIX}{spec.agent_key}"

    async def _run(task: str, runtime: ToolRuntime[ChatContext]) -> ToolMessage:
        scope = runtime.context if isinstance(runtime.context, ChatContext) else ChatContext()
        sink = scope.agent_sink
        writer = runtime.stream_writer
        if sink is not None:
            sink.emit(AgentEvent.of_dispatch(spec.agent_key, spec.name, task))
        status = "success"
        streamed: list[str] = []
        official = ""
        try:
            async for mode, chunk in graph.astream(
                {"messages": [HumanMessage(content=task)]},
                config=_subgraph_config(scope, spec.agent_key, stateful=stateful),
                context=scope,
                stream_mode=["messages", "updates"],
            ):
                if mode == "messages":
                    message_chunk, _metadata = chunk
                    if isinstance(message_chunk, AIMessageChunk):
                        text = message_text(message_chunk.content)
                        if text:
                            streamed.append(text)
                            if writer is not None:
                                # 执行智能体的 token 直接并入主 SSE 流: 护栏改写发生在
                                # 流式输出之后, 这条转发是「照实改写 + 明确告知」语义的
                                # 多智能体版本(见 app/middleware/agent_pedagogy.py)。
                                writer(
                                    {
                                        "type": "executor_text",
                                        "agent_key": spec.agent_key,
                                        "text": text,
                                    }
                                )
                else:  # updates: 取护栏改写后的最终 AI 文本作为给总管的「官方结论」
                    for node_output in chunk.values():
                        for message in (node_output or {}).get("messages", []):
                            if isinstance(message, AIMessage) and not message.tool_calls:
                                text = message_text(message.content)
                                if text:
                                    official = text
        except Exception:  # noqa: BLE001 - 子图失败只影响这一次调度
            logger.exception("执行智能体 %s 运行失败", spec.agent_key)
            status = "error"
            official = f"{spec.name} 执行失败, 请稍后重试, 或改由其他智能体处理。"
        if sink is not None:
            sink.emit(AgentEvent.of_return(spec.agent_key, spec.name, task, status))
        answer = official or "".join(streamed) or f"{spec.name} 没有返回内容。"
        # already_streamed: 全文已实时推给用户, 总管(mock 与真实模型的系统提示都
        # 被告知)不应复述, 否则前端会出现两段一模一样的回答。
        return ToolMessage(
            content=answer,
            name=name,
            tool_call_id=getattr(runtime, "tool_call_id", "") or "",
            additional_kwargs={"already_streamed": bool(streamed)},
        )

    _run.__name__ = name
    _run.__doc__ = (
        f"把子任务交给执行智能体「{spec.name}」({spec.agent_key})并返回它的结论。"
        f"{spec.description} task 用一句话写清要它做什么、范围与时间窗。"
    )
    return tool(_run)


async def build_runtime(specs: tuple[AgentSpec, ...], checkpointer: Any) -> AgentRuntime:
    """按注册表快照编译总管 + 执行智能体子图(不读 DB, 纯构造)。"""
    settings = get_settings()
    executors: dict[str, Any] = {}
    for spec in specs:
        if spec.is_supervisor or not spec.is_active:
            continue
        tools = _resolve_tools(spec.tool_refs, owner=spec.agent_key)
        middleware: list[Any] = [SkillMiddleware(allowed=spec.skill_refs or None)]
        stateful = spec.agent_key == LEARNING_AGENT_KEY
        if stateful:
            # 教学频道挂在子图上: 派生线程 + 主 checkpointer, 跨轮载体不新增任何存储。
            middleware.append(PedagogyMiddleware())
        if spec.skill_refs:
            tools = [*tools, *SKILL_TOOLS]
        executors[spec.agent_key] = create_agent(
            model=_resolve_model(settings),
            tools=tools,
            system_prompt=spec.system_prompt or settings.system_prompt,
            middleware=middleware,
            context_schema=ChatContext,
            checkpointer=checkpointer if stateful else None,
        )

    supervisor_spec = next(spec for spec in specs if spec.is_supervisor)
    handoff_tools = [
        _build_handoff_tool(spec, executors[spec.agent_key], stateful=spec.agent_key == LEARNING_AGENT_KEY)
        for spec in specs
        if not spec.is_supervisor and spec.is_active and spec.agent_key in executors
    ]
    supervisor_tools = [
        *_resolve_tools(supervisor_spec.tool_refs, owner=supervisor_spec.agent_key),
        *handoff_tools,
    ]
    if supervisor_spec.skill_refs:
        supervisor_tools = [*supervisor_tools, *SKILL_TOOLS]
    supervisor = create_agent(
        model=_resolve_model(settings),
        tools=supervisor_tools,
        system_prompt=(supervisor_spec.system_prompt or settings.system_prompt)
        + SUPERVISOR_STREAM_NOTE,
        # SkillMiddleware 先追加技能目录, DispatchMiddleware 再追加教学调度提示;
        # 主图不再挂 PedagogyMiddleware(频道在 edu-agent 子图上)。
        middleware=[
            SkillMiddleware(allowed=supervisor_spec.skill_refs or None),
            DispatchMiddleware(),
        ],
        context_schema=ChatContext,
        checkpointer=checkpointer,
    )
    logger.info(
        "Multi-agent runtime ready (provider=%s, model=%s, executors=%s, handoffs=%d)",
        settings.llm_provider,
        settings.llm_model,
        sorted(executors),
        len(handoff_tools),
    )
    return AgentRuntime(
        supervisor=supervisor,
        supervisor_spec=supervisor_spec,
        executors=executors,
        specs=specs,
        handoff_tool_names=tuple(item.name for item in handoff_tools),
    )


_runtime: AgentRuntime | None = None
_runtime_lock: asyncio.Lock | None = None
_runtime_lock_loop: Any = None


def _runtime_lock_for(loop: Any) -> asyncio.Lock:
    """按事件循环取构建锁: asyncio 原语会绑定首次使用它的循环。

    测试里每个 ``TestClient`` 上下文是一个新循环, 进程级复用同一把锁会抛
    "bound to a different event loop"; 生产只有一个循环, 行为不变。
    换循环意味着旧循环已退出(不再有人持锁), 直接换锁是安全的。
    """
    global _runtime_lock, _runtime_lock_loop
    if _runtime_lock is None or _runtime_lock_loop is not loop:
        _runtime_lock = asyncio.Lock()
        _runtime_lock_loop = loop
    return _runtime_lock


def invalidate_runtime() -> None:
    """注册表写操作后调用: 下一次聊天按新注册表重建(启停即时生效)。"""
    global _runtime
    _runtime = None


async def get_runtime(session: Any | None = None) -> AgentRuntime:
    """进程级运行时单例: 注册表 -> 图。``session`` 透传给注册表读取(测试用)。"""
    global _runtime
    if _runtime is None:
        async with _runtime_lock_for(asyncio.get_running_loop()):
            if _runtime is None:
                specs = await load_agent_specs(session)
                checkpointer = await init_checkpointer()
                _runtime = await build_runtime(specs, checkpointer)
    return _runtime


async def get_agent() -> Any:
    """聊天入口 = 总管子图(保留旧名: threads API 与既有测试按它取主图)。"""
    runtime = await get_runtime()
    return runtime.supervisor


if __name__ == "__main__":
    async def _demo() -> None:
        agent = await get_agent()
        user_message = {"role": "user", "content": "你是谁"}
        config = {"configurable": {"thread_id": "123"}}
        ret = await agent.ainvoke(input=user_message, config=config)
        print(ret)
        await close_checkpointer()

    asyncio.run(_demo())
