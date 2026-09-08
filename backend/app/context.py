"""Agent 运行期上下文(context_schema)。

``get_agent()`` 是进程级单例(lru_cache), 系统提示与工具集在构造时就固定了;
因此**任何按用户/按请求的差异**(如可见技能、权限)都不能烧进 agent 实例,
只能通过 ``create_agent(context_schema=ChatContext)`` + 调用时 ``context=`` 传入,
在 middleware / 工具里经 ``runtime.context`` 读取。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True, frozen=True)
class ChatContext:
    """一次 agent 调用的身份上下文。

    - ``authenticated=False``(匿名演示模式)时只能看到 ``visibility: public`` 的技能;
    - ``permissions`` 直接来自 RBAC(``AuthContext.permissions``), 含 ``*`` 通配。
    """

    account_id: int = 0
    authenticated: bool = False
    permissions: frozenset[str] = field(default_factory=frozenset)
