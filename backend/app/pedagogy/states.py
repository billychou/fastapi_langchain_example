"""费曼循环的状态枚举与转移表(见 ``docs/pedagogy-core.md`` §1)。

设计原则: **状态机放服务端, 不放提示词** —— 一次 ``/api/chat`` 请求 = 状态机的一次
``tick``; Agent 只负责生成自然语言, **状态转移由内核裁决**。

本模块刻意不依赖 pydantic / 数据库 / LLM: 只有枚举和一张声明式转移表,
因此可以在没有任何外部服务的情况下被穷举测试。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal


class FeynmanState(StrEnum):
    """费曼循环七态(值即 SSE ``learn.state`` 事件里的字符串)。"""

    PICK = "Pick"
    TEACH = "Teach"
    DETECT = "Detect"
    RETURN = "Return"
    SIMPLIFY = "Simplify"
    CLOSED = "Closed"
    ESCAPED = "Escaped"


#: 终态: 进入后不再接受任何事件(要重开必须新建一轮循环)。
TERMINAL_STATES: frozenset[FeynmanState] = frozenset(
    {FeynmanState.CLOSED, FeynmanState.ESCAPED}
)

#: 非终态: 逃生舱(escape_hatch)在这些状态下都可被请求。
ACTIVE_STATES: tuple[FeynmanState, ...] = (
    FeynmanState.PICK,
    FeynmanState.TEACH,
    FeynmanState.DETECT,
    FeynmanState.RETURN,
    FeynmanState.SIMPLIFY,
)

EventKind = Literal[
    "concept_named",
    "explanation_submitted",
    "gap_reported",
    "supply_completed",
    "simplify_judged",
    "escape_requested",
]


def _build_transitions() -> dict[tuple[FeynmanState, EventKind], tuple[FeynmanState, ...]]:
    """构造转移表。

    值是按判定优先级排列的**候选**目标状态: 具体落到哪个由
    :func:`app.pedagogy.loop.tick` 依据事件载荷与 :class:`PedagogyConfig` 裁决
    (例如 ``Teach`` 收到字数不足的自述时留在原状态)。表本身只声明「哪些边存在」,
    是穷举测试与设计文档的唯一事实来源。
    """
    table: dict[tuple[FeynmanState, EventKind], tuple[FeynmanState, ...]] = {
        (FeynmanState.PICK, "concept_named"): (FeynmanState.TEACH,),
        # 自述字数达标 -> Detect; 不达标 -> 留在 Teach 继续索要
        (FeynmanState.TEACH, "explanation_submitted"): (
            FeynmanState.DETECT,
            FeynmanState.TEACH,
        ),
        # 有未闭合 gap -> Return 补给; 无 gap(或学习者自己修正) -> Simplify
        (FeynmanState.DETECT, "gap_reported"): (
            FeynmanState.RETURN,
            FeynmanState.SIMPLIFY,
        ),
        (FeynmanState.RETURN, "supply_completed"): (FeynmanState.TEACH,),
        # 简化+类比+反例+迁移题全过 -> Closed; 未过 -> 回 Teach;
        # 触到 MAX_LOOPS 上限时留在 Simplify 并要求升级策略(escalate), 不再空转
        (FeynmanState.SIMPLIFY, "simplify_judged"): (
            FeynmanState.CLOSED,
            FeynmanState.TEACH,
            FeynmanState.SIMPLIFY,
        ),
    }
    # 逃生舱: 任意非终态都可请求直答; 未确认则留在原状态(escape_hatch 护栏)
    for state in ACTIVE_STATES:
        table[(state, "escape_requested")] = (FeynmanState.ESCAPED, state)
    return table


#: 转移表: ``(当前状态, 事件类型) -> 候选目标状态``。
TRANSITIONS: dict[tuple[FeynmanState, EventKind], tuple[FeynmanState, ...]] = _build_transitions()


def can(state: FeynmanState, kind: EventKind) -> bool:
    """该状态下这个事件是否是一条合法的边。"""
    return (state, kind) in TRANSITIONS


def allowed_events(state: FeynmanState) -> tuple[EventKind, ...]:
    """某状态可以接受的全部事件(终态返回空)。"""
    return tuple(kind for (src, kind) in TRANSITIONS if src == state)
