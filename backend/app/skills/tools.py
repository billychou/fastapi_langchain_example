"""技能工具: 渐进披露的 L1/L2/L3 入口。

三个工具在 ``create_agent`` 时**全量注册**(LangChain 1.x 不允许在 middleware 里临时
新增未注册工具), 再由 :class:`~app.middleware.agent_skills.SkillMiddleware` 决定本次
请求是否把它们暴露给模型。真正的授权判定在工具内部用
:meth:`SkillRegistry.authorize` 再做一次: 即使模型幻觉出一个技能名、或用户越权构造参数,
也拿不到内容。

调用者身份来自 ``runtime.context``(:class:`~app.context.ChatContext`), 由 /api/chat 注入。
"""

from __future__ import annotations

import logging

from langchain.tools import ToolRuntime, tool

from app.context import ChatContext
from app.skills.registry import SkillRegistry, get_skill_registry
from app.skills.security import SkillSecurityError

logger = logging.getLogger("skills.tools")


def _scope(runtime: ToolRuntime[ChatContext] | None) -> ChatContext:
    """取出本次调用的身份上下文; 无上下文时按匿名最小权限处理。"""
    context = getattr(runtime, "context", None)
    return context if isinstance(context, ChatContext) else ChatContext()


def _registry() -> SkillRegistry:
    return get_skill_registry()


def _denied(name: str) -> str:
    # 不区分「不存在」与「无权限」, 避免技能名枚举
    return f"技能不可用(不存在或当前账号无权限): {name}"


@tool
def list_skills(runtime: ToolRuntime[ChatContext]) -> str:
    """列出当前账号可用的全部技能(名称、版本、用途)。

    当系统提示里的技能目录被截断、或需要确认某类任务是否已有现成技能时调用。
    返回结果只是目录, 具体步骤仍需 load_skill 读取。
    """
    scope = _scope(runtime)
    skills = _registry().visible_to(
        permissions=scope.permissions, authenticated=scope.authenticated
    )
    if not skills:
        return "当前没有可用技能。请直接用通用能力回答用户问题。"
    lines = [
        f"- {item.name} (v{item.version}): {item.description}"
        + (f" [附件: {len(item.files)} 个]" if item.files else "")
        for item in skills
    ]
    logger.info(
        "list_skills account_id=%s visible=%d",
        scope.account_id,
        len(skills),
        extra={"skills": [item.name for item in skills]},
    )
    return "可用技能:\n" + "\n".join(lines)


@tool
def load_skill(name: str, runtime: ToolRuntime[ChatContext]) -> str:
    """读取指定技能的完整操作说明(SKILL.md 正文), 然后严格按其中步骤执行。

    name 必须是技能目录里出现过的名称(区分大小写与连字符)。
    不要凭技能名猜测内容, 也不要跳过这一步直接回答。
    """
    scope = _scope(runtime)
    registry = _registry()
    skill = registry.authorize(
        name, permissions=scope.permissions, authenticated=scope.authenticated
    )
    if skill is None:
        logger.warning(
            "load_skill denied account_id=%s skill=%s", scope.account_id, name or "-"
        )
        return _denied(name)
    logger.info(
        "load_skill account_id=%s skill=%s version=%s",
        scope.account_id,
        skill.name,
        skill.version,
    )
    return registry.render_body(skill)


@tool
def read_skill_file(name: str, path: str, runtime: ToolRuntime[ChatContext]) -> str:
    """读取某个技能目录内的附件文本(如 references/checklist.md)。

    name 为技能名, path 为技能目录内的相对路径(不接受绝对路径与 ..)。
    仅在技能说明要求查阅附件、或需要模板/清单/标准原文时调用。
    """
    scope = _scope(runtime)
    registry = _registry()
    skill = registry.authorize(
        name, permissions=scope.permissions, authenticated=scope.authenticated
    )
    if skill is None:
        logger.warning(
            "read_skill_file denied account_id=%s skill=%s", scope.account_id, name or "-"
        )
        return _denied(name)
    try:
        content = registry.read_file(skill, path)
    except SkillSecurityError as exc:
        logger.warning(
            "read_skill_file blocked account_id=%s skill=%s path=%s reason=%s",
            scope.account_id,
            skill.name,
            path,
            exc,
        )
        return f"无法读取技能附件: {exc}"
    logger.info(
        "read_skill_file account_id=%s skill=%s path=%s chars=%d",
        scope.account_id,
        skill.name,
        path,
        len(content),
    )
    return f"[{skill.name}/{path}]\n{content}"


SKILL_TOOLS = [list_skills, load_skill, read_skill_file]
SKILL_TOOL_NAMES = frozenset(item.name for item in SKILL_TOOLS)
