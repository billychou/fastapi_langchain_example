"""教学法内核(学科无关): 费曼循环状态机 + 卡点分类 + 掌握度模型。

设计文档: ``docs/pedagogy-core.md``(规格)、``docs/education-agent-architecture.md`` §3(落位)。

分层约定 —— 越往下越纯, 依赖越少:

- :mod:`app.pedagogy.states`  七态枚举 + 声明式转移表(不依赖 pydantic);
- :mod:`app.pedagogy.schemas` 证据的数据形状(Atom / Claim / GapReport / 事件 / 状态快照);
- :mod:`app.pedagogy.policy`  五条护栏 + ``PEDAGOGY_*`` 阈值(纯函数);
- :mod:`app.pedagogy.loop`    ``tick(state, event) -> LoopStep`` 纯函数, **裁决权在这里**;
- :mod:`app.pedagogy.gaps`    判分器: 讲解 -> claims -> 四类卡点(唯一会调 LLM 的一层);
- :mod:`app.pedagogy.mastery` 掌握度更新/衰减/复习调度(纯函数)。

本包**不碰数据库、不发 HTTP**; 状态由 API/service 层装配后经 ``ChatContext`` 传入
(与 skills 的「身份经 context 传入」同构, 不破坏 ``get_agent()`` 单例约束)。
"""

from __future__ import annotations

from app.pedagogy.loop import Action, LoopStep, closure_verdict, tick
from app.pedagogy.policy import (
    DIRECT_ANSWER_REWRITE_HINT,
    Guard,
    GuardAction,
    PedagogyConfig,
    Violation,
    check_talk_ratio,
    enforce_monologue_cap,
    enforce_no_direct_answer,
    enforce_probe_budget,
    escape_violation,
    find_solution_start,
    talk_ratio,
)
from app.pedagogy.schemas import (
    Atom,
    AtomBundle,
    AtomKind,
    AtomSource,
    Claim,
    ClaimStatus,
    ConceptNamed,
    EscapeRequested,
    ExplanationSubmitted,
    ExplanationTurn,
    GapItem,
    GapReport,
    GapReported,
    GapScores,
    GapType,
    LoopEvent,
    MissingEdge,
    PedagogyState,
    SimplifyJudged,
    SupplyCompleted,
    Verdict,
)
from app.pedagogy.states import (
    ACTIVE_STATES,
    TERMINAL_STATES,
    TRANSITIONS,
    EventKind,
    FeynmanState,
    allowed_events,
    can,
)

__all__ = [
    "ACTIVE_STATES",
    "DIRECT_ANSWER_REWRITE_HINT",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "Action",
    "Atom",
    "AtomBundle",
    "AtomKind",
    "AtomSource",
    "Claim",
    "ClaimStatus",
    "ConceptNamed",
    "EscapeRequested",
    "EventKind",
    "ExplanationSubmitted",
    "ExplanationTurn",
    "FeynmanState",
    "GapItem",
    "GapReport",
    "GapReported",
    "GapScores",
    "GapType",
    "Guard",
    "GuardAction",
    "LoopEvent",
    "LoopStep",
    "MissingEdge",
    "PedagogyConfig",
    "PedagogyState",
    "SimplifyJudged",
    "SupplyCompleted",
    "Verdict",
    "Violation",
    "allowed_events",
    "can",
    "check_talk_ratio",
    "closure_verdict",
    "enforce_monologue_cap",
    "enforce_no_direct_answer",
    "enforce_probe_budget",
    "escape_violation",
    "find_solution_start",
    "talk_ratio",
    "tick",
]
