"""总管调度提示中间件(LangChain AgentMiddleware, 不是 Starlette 中间件)。

教学回合的判读发生在**路由层**(``prepare_turn``): 它能在学习者这句话进模型之前
就决定「这一轮是教学回合」并产出状态卡。但多智能体化之后, 教学状态块挂在
``edu-agent`` 子图上 —— 总管若不知道该把这句话调度过去, 状态块就永远没机会注入,
费曼循环会在总管这一层被静默丢掉。

本中间件只做一件事: 当 ``ChatContext.pedagogy_state`` 存在、且总管绑定了教学智能体
的 handoff 工具时, 往系统提示里追加一段调度指令, 其中含机器标记
``[dispatch:call_<key>]``(mock 模型据此确定性地复现调度, 真实模型读自然语言部分)。

``pedagogy_state is None``(闲聊/匿名/降级关闭)时**零改动**, 与注入前逐字一致。
"""

from __future__ import annotations

import logging

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain_core.messages import SystemMessage

from app.context import ChatContext

logger = logging.getLogger("agent.dispatch")

#: 默认教学智能体 key; 与 app/agent.py 的 LEARNING_AGENT_KEY 保持一致(由测试对账)。
DEFAULT_LEARNING_AGENT_KEY = "edu-agent"


def _scope(context: object) -> ChatContext:
    return context if isinstance(context, ChatContext) else ChatContext()


def dispatch_hint(agent_key: str, agent_name: str) -> str:
    """追加到总管系统提示的调度指令(含 mock 用的机器标记)。"""
    return (
        f"\n\n[dispatch:call_{agent_key}]\n"
        f"## dispatch_hint(服务端判定, 本轮唯一事实来源)\n"
        f"本轮已被服务端判定为**教学/学习会话**(学习者正在解释或追问某个概念)。\n"
        f"请把用户这句话**原样**交给执行智能体「{agent_name}」(工具 call_{agent_key}), "
        f"不要自行回答、不要改写概念名; 教学状态与护栏只存在于该智能体内部。"
    )


class DispatchMiddleware(AgentMiddleware[None, ChatContext]):
    """教学回合把总管推向教学智能体; 其余回合完全透明。"""

    def __init__(self, learning_key: str = DEFAULT_LEARNING_AGENT_KEY) -> None:
        self.learning_key = learning_key

    async def awrap_model_call(self, request: ModelRequest, handler):
        scope = _scope(request.runtime.context)
        tool_name = f"call_{self.learning_key}"
        bound = any(getattr(item, "name", "") == tool_name for item in request.tools)
        if scope.pedagogy_state is None or not bound:
            return await handler(request)
        hint = dispatch_hint(self.learning_key, _display_name(request, tool_name))
        system_message = request.system_message
        base = system_message.content if isinstance(system_message, SystemMessage) else ""
        base = base if isinstance(base, str) else ""
        system_message = SystemMessage(content=f"{base}{hint}" if base else hint.strip("\n"))
        logger.debug("dispatch hint injected tool=%s account_id=%s", tool_name, scope.account_id)
        return await handler(request.override(system_message=system_message))


def _display_name(request: ModelRequest, tool_name: str) -> str:
    """从 handoff 工具的描述里取展示名(「K8s 集群诊断」式), 取不到就用 key。"""
    for item in request.tools:
        if getattr(item, "name", "") == tool_name:
            description = getattr(item, "description", "") or ""
            if "「" in description and "」" in description:
                return description.split("「", 1)[1].split("」", 1)[0]
    return tool_name.removeprefix("call_")
