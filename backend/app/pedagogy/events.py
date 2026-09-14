"""学习事件的对外形状(``learn`` SSE) + 单请求收集器 + 跨轮载体。

三件事, 都刻意**不碰数据库、不发 HTTP**(与 ``app.pedagogy`` 其余模块同一约束):

- :class:`LearnEvent`  一条 ``learn`` 事件: 前端「学习证据面板」的数据源, 与既有
  ``agent`` 工具链事件同构(顶层平铺, 只读 ``choices[].delta`` 的老客户端自动忽略);
- :class:`PedagogySink` 一次请求内的事件收集器。agent 是进程级单例, 中间件**不能**持有
  请求态, 所以收集器由路由创建后经 ``ChatContext`` 传入(与「身份经 context 传入」同构);
- :class:`LoopCarrier`  跨轮持久化的循环载体。存放位置是 LangGraph checkpointer 的
  ``pedagogy`` 频道(architecture §5 的 L1 记忆), 因此**不新增任何存储**, 且天然按
  ``thread_id`` 隔离。
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.pedagogy.policy import Violation
from app.pedagogy.schemas import GapItem, PedagogyState
from app.pedagogy.states import FeynmanState

logger = logging.getLogger("pedagogy.events")

#: ``learn`` 事件的类型。前端按类型渲染, 未知类型应被忽略(向前兼容)。
LearnEventType = Literal[
    "state",  # 循环快照: 当前状态 / 第几圈 / 内核要求的动作 / 推荐 persona
    "gap",  # 一个卡点: 类型 + 定位 + 下一步该问什么
    "guard",  # 护栏触发: 哪条护栏、内核做了什么、改写后的文本
    "attachment",  # monologue_cap 的溢出内容(卡片, 不塞进对话)
    "mastery",  # 掌握度增量(闭合时由 service 层填, P0 #7/#8)
    "note",  # 内核想告诉学习者/前端的一句话(不是错误)
]


class LearnEvent(BaseModel):
    """一条学习证据事件。

    ``data`` 是类型特定载荷, 只放 JSON 原生类型(list/dict/str/int/float/bool/None),
    这样 ``to_sse()`` 不需要任何自定义编码器, 前端也不需要知道 pydantic 模型。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: LearnEventType
    state: FeynmanState | None = None
    loop_index: int | None = None
    data: dict[str, Any] = Field(default_factory=dict)

    def to_sse(self) -> dict[str, Any]:
        """SSE ``learn`` 字段的形状(省略空值, 与 ``agent`` 事件同样紧凑)。"""
        payload: dict[str, Any] = {"type": self.type}
        if self.state is not None:
            payload["state"] = self.state.value
        if self.loop_index is not None:
            payload["loop_index"] = self.loop_index
        if self.data:
            payload["data"] = self.data
        return payload

    # ------------------------------------------------------------------ 构造器

    @classmethod
    def of_state(
        cls,
        state: PedagogyState,
        *,
        actions: tuple[str, ...] = (),
        reason: str = "",
        persona: str = "",
        probes_left: int = 0,
        loop_capped: bool | None = None,
        notes: tuple[str, ...] = (),
    ) -> LearnEvent:
        """循环快照: 前端据此显示「现在在哪一步、内核要我做什么」。

        ``loop_capped`` 缺省时直接读 ``state.loop_capped``: 状态块会告诉模型「已触顶」,
        事件却漏报的话, 学习者看到的界面与模型的行为就会互相矛盾。
        """
        capped = state.loop_capped if loop_capped is None else loop_capped
        data: dict[str, Any] = {
            "concept_id": state.concept_id,
            "actions": list(actions),
            "probes_left": probes_left,
            "talk_ratio": round(state.talk_ratio, 3),
            "open_gaps": [gap.type for gap in state.open_gaps],
        }
        if reason:
            data["reason"] = reason
        if persona:
            data["persona"] = persona
        if capped:
            data["loop_capped"] = True
        if notes:
            data["notes"] = list(notes)
        if state.last_scores is not None:
            data["scores"] = state.last_scores.model_dump(mode="json")
        return cls(
            type="state", state=state.state, loop_index=state.loop_index, data=data
        )

    @classmethod
    def of_gap(cls, gap: GapItem, state: PedagogyState) -> LearnEvent:
        """一个卡点 = 类型 + 定位 + 可执行的追问(不是一句「理解不够」)。"""
        data: dict[str, Any] = {
            "gap_type": gap.type,
            "probe": gap.probe,
            "severity": gap.severity,
        }
        if gap.claim_id:
            data["claim_id"] = gap.claim_id
        if gap.missing_edge is not None:
            data["missing_edge"] = gap.missing_edge.model_dump(mode="json", by_alias=True)
        if gap.supply is not None:
            data["supply_atom_ids"] = [atom.id for atom in gap.supply.atoms]
            data["supply_max_chars"] = gap.supply.max_chars
        return cls(type="gap", state=state.state, loop_index=state.loop_index, data=data)

    @classmethod
    def of_guard(
        cls,
        violation: Violation,
        state: PedagogyState,
        *,
        replacement: str | None = None,
    ) -> LearnEvent:
        """护栏触发。``replacement`` 是内核改写后的文本: 前端应当用它替换本条气泡。

        流式输出已经把原文推给了浏览器, 服务端无法收回 —— 所以护栏在 P0 的做法是
        「照实改写 + 明确告知」, 而不是假装没发生(取舍见 ``agent_pedagogy`` 模块文档)。
        """
        data: dict[str, Any] = {
            "guard": violation.guard.value,
            "action": violation.action.value,
            "detail": violation.detail,
        }
        if replacement is not None:
            data["replacement"] = replacement
        return cls(type="guard", state=state.state, loop_index=state.loop_index, data=data)

    @classmethod
    def of_attachment(
        cls, state: PedagogyState, *, title: str, text: str
    ) -> LearnEvent:
        """``monologue_cap`` 的溢出内容: 转成卡片, 不塞进对话流。"""
        return cls(
            type="attachment",
            state=state.state,
            loop_index=state.loop_index,
            data={"title": title, "text": text, "chars": len(text)},
        )

    @classmethod
    def of_mastery(
        cls,
        state: PedagogyState,
        *,
        atom_key: str,
        before: float,
        after: float,
        review_in_days: float | None = None,
        review_stage: int | None = None,
    ) -> LearnEvent:
        """掌握度增量(闭合时)。P0 #7/#8 由 service 层填, 形状先定死以便前端对接。"""
        data: dict[str, Any] = {
            "atom_key": atom_key,
            "before": round(before, 4),
            "after": round(after, 4),
            "delta": round(after - before, 4),
        }
        if review_in_days is not None:
            data["review_in_days"] = round(review_in_days, 2)
        if review_stage is not None:
            data["review_stage"] = review_stage
        return cls(type="mastery", state=state.state, loop_index=state.loop_index, data=data)

    @classmethod
    def of_note(cls, text: str, state: PedagogyState | None = None) -> LearnEvent:
        """内核的一句说明(例如「本轮发言需要判分器才能推进」)。"""
        return cls(
            type="note",
            state=state.state if state is not None else None,
            loop_index=state.loop_index if state is not None else None,
            data={"text": text},
        )


class PedagogySink:
    """一次请求内的 ``learn`` 事件收集器(可变; 经 ``ChatContext`` 传入单例 agent)。

    - ``emit`` 由中间件调用; ``drain`` 由 SSE 路由增量拉取(因此事件能与 token 增量
      交错输出, 而不是等到流结束才一次性吐出);
    - ``mark`` 提供幂等标记: 一次请求里模型可能被调用多次(工具循环), 同一件事
      (注入状态块、给某条消息记账)只应发生一次;
    - ``carrier`` 是本轮的**运行中载体**: 路由装配时放入学习者侧 tick 的结果, 中间件
      每处理一个教练侧事件就续写它, 最后由 ``aafter_model`` 写回 checkpointer 频道。
      放在这里而不是中间件实例上, 是因为 agent 是进程级单例 —— 中间件**不能**持有请求态。
    """

    __slots__ = ("_carrier", "_cursor", "_events", "_seen")

    def __init__(self) -> None:
        self._events: list[LearnEvent] = []
        self._cursor = 0
        self._seen: set[str] = set()
        self._carrier: LoopCarrier | None = None

    @property
    def carrier(self) -> LoopCarrier | None:
        return self._carrier

    @carrier.setter
    def carrier(self, value: LoopCarrier | None) -> None:
        self._carrier = value

    def emit(self, event: LearnEvent) -> None:
        self._events.append(event)

    def extend(self, events: list[LearnEvent] | tuple[LearnEvent, ...]) -> None:
        self._events.extend(events)

    def drain(self) -> list[LearnEvent]:
        """取出尚未被取走的事件(可重复调用, 无事件时返回空列表)。"""
        pending = self._events[self._cursor :]
        self._cursor = len(self._events)
        return pending

    def mark(self, key: str) -> bool:
        """首次出现返回 ``True`` 并记下, 之后返回 ``False``。"""
        if key in self._seen:
            return False
        self._seen.add(key)
        return True

    @property
    def emitted(self) -> int:
        return len(self._events)

    def __len__(self) -> int:
        return len(self._events)

    def __bool__(self) -> bool:
        """恒为 ``True``: sink 是服务对象而不是集合。

        定义了 ``__len__`` 之后, ``if sink:`` 会在「本轮还没产出事件」时为假 ——
        那正是教学刚开始的常态, 一个笔误就会静默关掉整个内核。判空请写
        ``sink is None``。
        """
        return True


class LoopCarrier(BaseModel):
    """跨轮持久化的循环载体(checkpointer ``pedagogy`` 频道的值)。

    除状态快照外只多带两个字段:

    - ``awaiting_escape``: 上一轮是否已经就「直答」向学习者要过确认。
      ``escape_hatch`` 护栏要求先确认再直答, 而「问过没有」是跨轮事实, 必须存下来,
      否则每次都只能停在「请确认」而永远出不去;
    - ``turns``: 这个线程上跑过多少轮教学请求(可观测性; 不参与裁决)。
    """

    model_config = ConfigDict(frozen=True)

    state: PedagogyState
    awaiting_escape: bool = False
    turns: int = Field(default=0, ge=0)

    def to_channel(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @classmethod
    def from_channel(cls, raw: Any) -> LoopCarrier | None:
        """宽容解析: 频道里的数据坏了就当作没有(退回 ``None``), 不让 ``/api/chat`` 变 500。

        状态机是**可重建的辅助信息**, 不是账本(账本在 MySQL 的 ``learner_explanation`` /
        ``mastery_ledger``)。所以这里的取舍是「宁可从 Pick 重新开始, 也不要中断对话」。
        """
        if not isinstance(raw, dict):
            return None
        try:
            return cls.model_validate(raw)
        except ValidationError as exc:
            logger.warning("pedagogy 频道数据无法解析, 本轮从 Pick 重新开始: %s", exc)
            return None
