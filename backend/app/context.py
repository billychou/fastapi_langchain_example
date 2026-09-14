"""Agent 运行期上下文(context_schema)。

``get_agent()`` 是进程级单例(lru_cache), 系统提示与工具集在构造时就固定了;
因此**任何按用户/按请求的差异**(如可见技能、权限、教学状态)都不能烧进 agent 实例,
只能通过 ``create_agent(context_schema=ChatContext)`` + 调用时 ``context=`` 传入,
在 middleware / 工具里经 ``runtime.context`` 读取。

教学字段(``pedagogy_*``)由 ``app/api/chat.py`` 用 :func:`app.pedagogy.session.prepare_turn`
装配后传入, agent 内部**不查库、不读写 checkpointer**(读在路由, 写在中间件的状态更新里)。
这与 skills 的「身份经 context 传入」是同一条约束的两个实例, 见
``docs/education-agent-architecture.md`` §6 要点 4。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 这三个类型**必须在运行期可导入**, 不能放进 TYPE_CHECKING 分支:
# 技能工具的签名是 ``runtime: ToolRuntime[ChatContext]``, pydantic 会为工具生成 args
# schema, 于是它要在运行期解析 ``ChatContext`` 的字段注解。放在 TYPE_CHECKING 里时
# 注解解析不到 ``PedagogyState``, 工具调用会抛
# ``PydanticUserError: `load_skill` is not fully defined``(而且只在真正调用工具时才炸,
# 导入期和建 agent 时都看不出来)。
# 依赖方向依然是单向的: pedagogy 从不 import context(它靠纯函数 + 显式传参), 这里
# 只是「上下文携带内核的数据形状」, 不构成环。
from app.pedagogy.events import PedagogySink
from app.pedagogy.loop import LoopStep
from app.pedagogy.schemas import PedagogyState


@dataclass(slots=True, frozen=True)
class ChatContext:
    """一次 agent 调用的身份 + 教学上下文。

    - ``authenticated=False``(匿名演示模式)时只能看到 ``visibility: public`` 的技能;
    - ``permissions`` 直接来自 RBAC(``AuthContext.permissions``), 含 ``*`` 通配;
    - ``pedagogy_sink is None`` 表示这一轮不是教学回合(闲聊/匿名/降级开关关闭),
      ``PedagogyMiddleware`` 据此完全跳过 —— 普通聊天的行为与注入前**逐字一致**。
    """

    account_id: int = 0
    authenticated: bool = False
    permissions: frozenset[str] = field(default_factory=frozenset)
    #: 学习者 ID。P0 与 ``account_id`` 同值(一人一学习者); P1 引入家长-学生绑定时
    #: 才会分叉, 所以字段现在就留出来, 免得届时改签名。
    learner_id: int = 0
    #: 本轮学习者侧 tick **之后**的状态快照(中间件把它渲染进系统提示)。
    pedagogy_state: PedagogyState | None = None
    #: 学习者侧事件触发的转移; ``None`` = 这一轮状态没动。
    pedagogy_step: LoopStep | None = None
    #: 本次请求的 ``learn`` 事件收集器(可变, 由路由 drain 成 SSE)。
    pedagogy_sink: PedagogySink | None = None
