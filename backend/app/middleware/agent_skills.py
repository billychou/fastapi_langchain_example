"""技能接入 agent 的中间件(LangChain AgentMiddleware, 不是 Starlette 中间件)。

职责两件事, 都在**请求级**完成, 因此单例 agent 也能按用户差异化:

1. ``awrap_model_call``: 把当前调用者可见的技能目录(L1)追加到系统提示, 并在
   没有任何可见技能时把技能工具从本次请求的工具集里摘掉(省 token, 也避免模型幻觉调用)。
2. ``awrap_tool_call``: 兜底拦截 —— 无可见技能却仍发起技能工具调用时直接返回错误消息,
   不进入工具实现。

注意: 本项目全异步(``astream``), LangChain 1.x 要求实现 ``a*`` 钩子;
只写同步版 ``wrap_model_call`` 会在异步调用时抛 NotImplementedError。
另外 ``request.override(tools=...)`` 只能传**已注册工具的子集**, 新增未注册工具会报错。
"""

from __future__ import annotations

import logging

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ToolCallRequest
from langchain_core.messages import SystemMessage, ToolMessage

from app.context import ChatContext
from app.skills.registry import SkillRegistry, get_skill_registry
from app.skills.tools import SKILL_TOOL_NAMES

logger = logging.getLogger("skills.middleware")


def _scope(context: object) -> ChatContext:
    return context if isinstance(context, ChatContext) else ChatContext()


class SkillMiddleware(AgentMiddleware[None, ChatContext]):
    """按调用者身份渲染技能目录并收敛技能工具的可见性。"""

    def __init__(self, registry: SkillRegistry | None = None) -> None:
        self.registry = registry or get_skill_registry()

    async def awrap_model_call(self, request: ModelRequest, handler):
        # registry.enabled=False 时 visible_to 直接返回空, 技能工具同样会被摘掉
        scope = _scope(request.runtime.context)
        visible = self.registry.visible_to(
            permissions=scope.permissions, authenticated=scope.authenticated
        )
        catalog = self.registry.render_catalog(visible)
        tools = request.tools
        if not visible:
            tools = [item for item in request.tools if item.name not in SKILL_TOOL_NAMES]

        system_message = request.system_message
        if catalog:
            base = system_message.content if isinstance(system_message, SystemMessage) else ""
            base = base if isinstance(base, str) else ""
            system_message = SystemMessage(content=f"{base}\n\n{catalog}" if base else catalog)

        return await handler(request.override(system_message=system_message, tools=tools))

    async def awrap_tool_call(self, request: ToolCallRequest, handler):
        tool_call = request.tool_call
        if tool_call.get("name") in SKILL_TOOL_NAMES:
            scope = _scope(request.runtime.context)
            visible = self.registry.visible_to(
                permissions=scope.permissions, authenticated=scope.authenticated
            )
            if not visible:
                logger.warning(
                    "skill tool call blocked (no visible skills) account_id=%s tool=%s",
                    scope.account_id,
                    tool_call.get("name"),
                )
                return ToolMessage(
                    content="当前账号没有可用技能, 请用通用能力回答。",
                    tool_call_id=tool_call.get("id") or "",
                    name=tool_call.get("name") or "",
                    status="error",
                )
        return await handler(request)
