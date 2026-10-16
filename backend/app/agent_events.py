"""多智能体调度事件的对外形状(``agent`` SSE) + 单请求收集器。

与 :mod:`app.pedagogy.events` 同构的两件事, 同样**不碰数据库、不发 HTTP**:

- :class:`AgentEvent`  一条调度事件: ``agent_dispatch``(总管把任务交给某执行智能体)
  或 ``agent_return``(执行智能体返回, 带 success/error)。前端思维链据此渲染
  「调度 → K8s 集群诊断」节点; 只读 ``choices[].delta`` 的老客户端自动忽略;
- :class:`AgentEventSink`  一次请求内的收集器。agent 是进程级单例, handoff 工具
  **不能**持有请求态, 所以收集器由路由创建后经 ``ChatContext.agent_sink`` 传入,
  路由在 SSE 循环里增量 drain(与 ``learn`` 通道同一手法)。

为什么不用 LangGraph 的 ``updates`` lane 代替: handoff 工具跑的是**子图**, 子图的
节点级更新不会冒泡到父图的 updates lane; 而调度本身(谁被叫去、成功没有)是平台级
可观测信息, 值得一个显式事件而不是让前端去猜工具名。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

#: ``agent`` 事件的类型。前端按类型渲染, 未知类型应被忽略(向前兼容)。
AgentEventType = Literal["agent_dispatch", "agent_return"]


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """一条调度事件; 字段全部 JSON 原生, ``to_sse`` 不需要自定义编码器。"""

    type: AgentEventType
    agent_key: str
    name: str
    task: str = ""
    status: str = ""  # agent_return: success / error

    def to_sse(self) -> dict[str, Any]:
        """SSE ``agent`` 字段的形状(省略空值, 与既有工具链事件同样紧凑)。"""
        payload: dict[str, Any] = {"type": self.type, "agent_key": self.agent_key, "name": self.name}
        if self.task:
            payload["task"] = self.task
        if self.status:
            payload["status"] = self.status
        return payload

    @classmethod
    def of_dispatch(cls, agent_key: str, name: str, task: str) -> AgentEvent:
        return cls(type="agent_dispatch", agent_key=agent_key, name=name, task=task)

    @classmethod
    def of_return(cls, agent_key: str, name: str, task: str, status: str) -> AgentEvent:
        return cls(type="agent_return", agent_key=agent_key, name=name, task=task, status=status)


class AgentEventSink:
    """一次请求内的调度事件收集器(可变; 经 ``ChatContext`` 传入单例 agent)。

    ``emit`` 由 handoff 工具调用; ``drain`` 由 SSE 路由增量拉取, 因此事件能与 token
    增量交错下发。与 ``PedagogySink`` 一样**不定义 ``__bool__`` 陷阱**: 判空请写
    ``sink is None``。
    """

    __slots__ = ("_cursor", "_events")

    def __init__(self) -> None:
        self._events: list[AgentEvent] = []
        self._cursor = 0

    def emit(self, event: AgentEvent) -> None:
        self._events.append(event)

    def drain(self) -> list[AgentEvent]:
        """取出尚未被取走的事件(可重复调用, 无事件时返回空列表)。"""
        pending = self._events[self._cursor :]
        self._cursor = len(self._events)
        return pending

    @property
    def emitted(self) -> int:
        return len(self._events)
