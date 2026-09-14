"""费曼循环状态机: ``tick(state, event) -> LoopStep``。

**这是一个纯函数**: 不发 HTTP、不查库、不调 LLM、不读时钟。同样的 ``(state, event,
config)`` 永远得到同样的 ``LoopStep``, 因此整条循环可以在 mock 下被穷举测试
(pedagogy-core §8)。持久化由 API/service 层负责: 把 ``LoopStep.state`` 写回
``thread_id`` 对应的记录即可(与 LangGraph checkpointer 同源, architecture §5)。

裁决权归属: 判分器(``gaps.py``)只提供证据(GapReport), **是否闭合由本模块按
rubric 阈值裁决** —— 不接受调用方直接塞一个 ``passed=True``。
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.pedagogy.policy import (
    PedagogyConfig,
    Violation,
    check_talk_ratio,
    enforce_probe_budget,
    escape_violation,
)
from app.pedagogy.schemas import (
    ConceptNamed,
    EscapeRequested,
    ExplanationSubmitted,
    GapItem,
    GapReport,
    GapReported,
    LoopEvent,
    PedagogyState,
    SimplifyJudged,
    SupplyCompleted,
)
from app.pedagogy.states import TRANSITIONS, FeynmanState


class Action(StrEnum):
    """内核裁决后要求调用方执行的动作(前端与技能据此决定下一步说什么)。"""

    NAME_CONCEPT = "name_concept"  # 要求命名概念 + 列出前置 Atom
    ASK_EXPLAIN = "ask_explain"  # 要求学习者用自己的话讲一遍
    ASK_MORE_DETAIL = "ask_more_detail"  # 自述字数不足, 继续索要
    PROBE = "probe"  # 进入诊断, 允许追问
    SUPPLY_ATOMS = "supply_atoms"  # 补给最小必要 Atom(带字数预算)
    REQUEST_SIMPLIFY = "request_simplify"  # 要求简化重讲 + 自造类比 + 反例 + 迁移题
    RECORD_MASTERY = "record_mastery"  # 闭合: 写掌握度账本
    SCHEDULE_REVIEW = "schedule_review"  # 闭合: 排期间隔复习
    ESCALATE = "escalate"  # 触到循环上限: 必须换策略(收窄范围或走逃生舱)
    CONFIRM_ESCAPE = "confirm_escape"  # 直答前的二次确认
    NO_MASTERY_CREDIT = "no_mastery_credit"  # 直答不计掌握度


class LoopStep(BaseModel):
    """一次 ``tick`` 的结果: 新快照 + 要做什么 + 触发了哪些护栏。"""

    model_config = ConfigDict(frozen=True)

    state: PedagogyState
    actions: tuple[Action, ...] = ()
    violations: tuple[Violation, ...] = ()
    notes: tuple[str, ...] = ()
    reason: str = ""

    @property
    def phase(self) -> FeynmanState:
        """转移后所处的状态(``step.state.state`` 的简写)。"""
        return self.state.state

    @property
    def terminal(self) -> bool:
        return self.state.terminal


def tick(
    state: PedagogyState, event: LoopEvent, *, config: PedagogyConfig | None = None
) -> LoopStep:
    """推进一次状态机。

    ``config`` 缺省时从 ``Settings``(``PEDAGOGY_*``)装配; 测试里应显式传入以固定阈值。
    非法事件与终态事件都**不抛异常**, 而是原样返回快照并说明原因 —— 模型输出的
    不确定性不应该让 ``/api/chat`` 变成 500。
    """
    cfg = config or PedagogyConfig.from_settings()
    kind = event.kind

    if state.terminal:
        return LoopStep(state=state, reason=f"循环已处于终态 {state.state}, 事件 {kind} 被忽略")

    if not TRANSITIONS.get((state.state, kind)):
        return LoopStep(
            state=state,
            reason=f"{state.state} 状态不接受 {kind} 事件(见 TRANSITIONS), 保持原状态",
        )

    if isinstance(event, ConceptNamed):
        return _on_concept_named(state, event)
    if isinstance(event, ExplanationSubmitted):
        return _on_explanation_submitted(state, event, cfg)
    if isinstance(event, GapReported):
        return _on_gap_reported(state, event, cfg)
    if isinstance(event, SupplyCompleted):
        return _on_supply_completed(state, event, cfg)
    if isinstance(event, SimplifyJudged):
        return _on_simplify_judged(state, event, cfg)
    if isinstance(event, EscapeRequested):
        return _on_escape_requested(state, event)
    return LoopStep(state=state, reason=f"未实现的事件类型 {kind}")  # pragma: no cover


# --------------------------------------------------------------------------- 各状态处理


def _on_concept_named(state: PedagogyState, event: ConceptNamed) -> LoopStep:
    """``Pick -> Teach``: 概念已命名 + 前置 Atom 已列出, 开始第一圈。"""
    nxt = state.model_copy(
        update={
            "state": FeynmanState.TEACH,
            "concept_id": event.concept_id,
            "prerequisites": event.prerequisites,
            "loop_index": state.loop_index + 1,
            "open_gaps": (),
            "probes_used": 0,
        }
    )
    return LoopStep(
        state=nxt,
        actions=(Action.ASK_EXPLAIN,),
        reason=f"概念 {event.concept_id} 已命名, 请学习者用自己的话讲一遍",
    )


def _on_explanation_submitted(
    state: PedagogyState, event: ExplanationSubmitted, cfg: PedagogyConfig
) -> LoopStep:
    """``Teach -> Detect``: 自述达标才进诊断, 否则留在 ``Teach`` 继续索要。"""
    talked = {
        "learner_chars": state.learner_chars + event.chars,
        "system_chars": state.system_chars + event.system_chars,
    }
    if event.chars < cfg.min_explain_chars:
        actions = [Action.ASK_MORE_DETAIL]
        if state.loop_index >= cfg.max_loops:
            actions.append(Action.ESCALATE)
        return LoopStep(
            state=state.model_copy(update=talked),
            actions=tuple(actions),
            reason=(
                f"自述 {event.chars} 字 < min_explain_chars={cfg.min_explain_chars}, "
                "留在 Teach 继续索要讲解"
            ),
        )

    ratio_violation = check_talk_ratio(talked["learner_chars"], talked["system_chars"], cfg)
    nxt = state.model_copy(
        update={**talked, "state": FeynmanState.DETECT, "probes_used": 0}
    )
    return LoopStep(
        state=nxt,
        actions=(Action.PROBE,),
        violations=(ratio_violation,) if ratio_violation else (),
        reason=f"收到 {event.chars} 字自述, 进入诊断",
    )


def _on_gap_reported(
    state: PedagogyState, event: GapReported, cfg: PedagogyConfig
) -> LoopStep:
    """``Detect -> Return | Simplify``: 有未闭合卡点就补给, 没有就进简化与迁移。"""
    report = event.report
    system_chars = state.system_chars + event.system_chars
    scores = {"last_scores": report.scores, "system_chars": system_chars}

    if report.verdict == "closed" or not report.gaps:
        nxt = state.model_copy(
            update={**scores, "state": FeynmanState.SIMPLIFY, "open_gaps": (), "probes_used": 0}
        )
        return LoopStep(
            state=nxt,
            actions=(Action.REQUEST_SIMPLIFY,),
            reason="无未闭合卡点, 进入简化重讲与迁移题",
        )

    # 严重度高的卡点优先占用追问预算与补给预算
    ordered = sorted(enumerate(report.gaps), key=lambda pair: (-pair[1].severity, pair[0]))
    probes, probe_violation = enforce_probe_budget([gap.probe for _, gap in ordered], cfg)
    gaps, clamp_note = _clamp_supply([gap for _, gap in ordered][: len(probes)], cfg)

    notes = [note for note in (clamp_note, probe_violation.detail if probe_violation else None) if note]
    nxt = state.model_copy(
        update={
            **scores,
            "state": FeynmanState.RETURN,
            "open_gaps": gaps,
            "probes_used": len(probes),
        }
    )
    return LoopStep(
        state=nxt,
        actions=(Action.SUPPLY_ATOMS,),
        violations=(probe_violation,) if probe_violation else (),
        notes=tuple(notes),
        reason=f"检出 {len(report.gaps)} 个卡点, 补给 {len(gaps)} 个后要求重讲",
    )


def _on_supply_completed(
    state: PedagogyState, event: SupplyCompleted, cfg: PedagogyConfig
) -> LoopStep:
    """``Return -> Teach``: 补给完成, 要求重讲(触到循环上限则升级策略)。"""
    base = {
        "system_chars": state.system_chars + event.system_chars,
        "open_gaps": (),
        "probes_used": 0,
    }
    if state.loop_index >= cfg.max_loops:
        return LoopStep(
            state=state.model_copy(update={**base, "loop_capped": True}),
            actions=(Action.ESCALATE,),
            notes=(f"已达循环上限 max_loops={cfg.max_loops}, 不再回到讲解",),
            reason="补给完成但循环已达上限, 需收窄范围或走逃生舱",
        )
    nxt = state.model_copy(
        update={**base, "state": FeynmanState.TEACH, "loop_index": state.loop_index + 1}
    )
    return LoopStep(
        state=nxt,
        actions=(Action.ASK_EXPLAIN,),
        reason=f"补给 {len(event.bundle.atoms)} 个 Atom, 进入第 {nxt.loop_index} 圈讲解",
    )


def _on_simplify_judged(
    state: PedagogyState, event: SimplifyJudged, cfg: PedagogyConfig
) -> LoopStep:
    """``Simplify -> Closed | Teach``: 由内核按 rubric 阈值裁决是否闭合。"""
    report = event.report
    system_chars = state.system_chars + event.system_chars
    passed, unmet = closure_verdict(report, event.transfer_passed, cfg)
    scores = {"last_scores": report.scores, "system_chars": system_chars}

    if passed:
        nxt = state.model_copy(
            update={**scores, "state": FeynmanState.CLOSED, "open_gaps": (), "probes_used": 0}
        )
        return LoopStep(
            state=nxt,
            actions=(Action.RECORD_MASTERY, Action.SCHEDULE_REVIEW),
            reason="迁移题通过且 rubric 达标, 概念闭合",
        )

    if state.loop_index >= cfg.max_loops:
        return LoopStep(
            state=state.model_copy(update={**scores, "loop_capped": True}),
            actions=(Action.ESCALATE,),
            notes=tuple(unmet),
            reason=f"未闭合且已达循环上限 max_loops={cfg.max_loops}, 需换策略",
        )

    nxt = state.model_copy(
        update={**scores, "state": FeynmanState.TEACH, "loop_index": state.loop_index + 1}
    )
    return LoopStep(
        state=nxt,
        actions=(Action.ASK_EXPLAIN,),
        notes=tuple(unmet),
        reason=f"未闭合(第 {nxt.loop_index} 圈), 回到讲解",
    )


def _on_escape_requested(state: PedagogyState, event: EscapeRequested) -> LoopStep:
    """逃生舱: 未确认 -> 留在原状态并要求确认; 已确认 -> ``Escaped`` 且不计掌握度。

    ``open_gaps`` 被刻意保留: 「在哪里放弃的」本身就是有价值的学习证据。
    """
    system_chars = state.system_chars + event.system_chars
    if not event.confirmed:
        violation = escape_violation(confirmed=False)
        return LoopStep(
            state=state.model_copy(update={"system_chars": system_chars}),
            actions=(Action.CONFIRM_ESCAPE,),
            violations=(violation,) if violation else (),
            reason="学习者请求直答但未确认, 先确认再直答",
        )
    nxt = state.model_copy(update={"state": FeynmanState.ESCAPED, "system_chars": system_chars})
    return LoopStep(
        state=nxt,
        actions=(Action.NO_MASTERY_CREDIT,),
        notes=("直答不计入掌握度; 可事后补测",),
        reason="学习者已确认直答, 循环结束(未产生掌握证据)",
    )


# --------------------------------------------------------------------------- 裁决辅助


def closure_verdict(
    report: GapReport, transfer_passed: bool, cfg: PedagogyConfig
) -> tuple[bool, list[str]]:
    """是否满足 ``verdict = closed`` 的全部条件(pedagogy-core §3 判分 rubric)。

    返回 ``(是否闭合, 未达标项列表)``; 未达标项会作为 ``LoopStep.notes`` 回传,
    让教练能说出「还差什么」而不是「再讲一遍」。
    """
    unmet: list[str] = []
    if not transfer_passed:
        unmet.append("迁移题未通过")
    if report.scores.completeness < cfg.min_completeness:
        unmet.append(
            f"completeness {report.scores.completeness:.2f} < {cfg.min_completeness:.2f}"
        )
    if report.scores.own_words < cfg.min_own_words:
        unmet.append(f"own_words {report.scores.own_words:.2f} < {cfg.min_own_words:.2f}")
    shields = report.gaps_of("jargon_shield")
    if shields:
        unmet.append(f"仍有 {len(shields)} 个术语盾(jargon_shield)卡点未拆")
    if report.wrong_claim_ids:
        unmet.append(f"有 {len(report.wrong_claim_ids)} 条命题与已验证 Atom 冲突(wrong)")
    if report.verdict != "closed":
        unmet.append("判分器结论为 not_closed")
    return (not unmet), unmet


def _clamp_supply(
    gaps: list[GapItem], cfg: PedagogyConfig
) -> tuple[tuple[GapItem, ...], str | None]:
    """把 ``Return`` 的补给总字数压到 ``return_max_chars`` 以内(按传入顺序分配预算)。"""
    budget = cfg.return_max_chars
    clamped: list[GapItem] = []
    changed = False
    for gap in gaps:
        if gap.supply is None:
            clamped.append(gap)
            continue
        allowed = max(0, min(gap.supply.max_chars, budget))
        budget -= allowed
        if allowed == gap.supply.max_chars:
            clamped.append(gap)
            continue
        changed = True
        clamped.append(
            gap.model_copy(update={"supply": gap.supply.model_copy(update={"max_chars": allowed})})
        )
    note = (
        f"补给总预算压到 return_max_chars={cfg.return_max_chars} 字以内" if changed else None
    )
    return tuple(clamped), note
