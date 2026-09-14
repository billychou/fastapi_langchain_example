"""``learn`` SSE 事件的形状 + 单请求收集器 + 跨轮载体。

三件事都是**契约**: 前端(#9)按 ``type`` 渲染证据面板, 中间件按 ``sink`` 交接请求态,
checkpointer 按 ``LoopCarrier`` 的 JSON 存跨轮事实。契约变了不会有运行时报错,
只会让面板静默少一块, 所以这里逐字段钉住。

关键取舍(与 ``agent_pedagogy`` 模块文档一致): ``of_guard`` 只有在真的改写了文本时
才带 ``replacement`` —— 前端据此决定要不要替换气泡, 空字符串与「没改写」必须可区分。
"""

from __future__ import annotations

import json

import pytest
from app.pedagogy.events import LearnEvent, LoopCarrier, PedagogySink
from app.pedagogy.policy import Guard, GuardAction, Violation
from app.pedagogy.schemas import (
    Atom,
    AtomBundle,
    GapItem,
    GapScores,
    MissingEdge,
    PedagogyState,
)
from app.pedagogy.states import FeynmanState
from pydantic import ValidationError

STATE = PedagogyState(
    thread_id="thread-events",
    account_id=7,
    learner_id=7,
    concept_id="闭包",
    state=FeynmanState.DETECT,
    loop_index=2,
    learner_chars=120,
    system_chars=80,
)


def gap_item(**overrides) -> GapItem:
    fields: dict = {
        "type": "causal_gap",
        "claim_id": "c1",
        "probe": "那一步为什么可以这么做?",
        "severity": 2,
    }
    fields.update(overrides)
    return GapItem(**fields)


def violation(guard: Guard = Guard.MONOLOGUE_CAP) -> Violation:
    return Violation(guard=guard, action=GuardAction.TRUNCATE, detail="超了")


# --------------------------------------------------------------------------- to_sse


def test_to_sse_omits_empty_fields():
    """空值一律省略: 事件要与既有 ``agent`` 事件一样紧凑(前端按字段存在性渲染)。"""
    assert LearnEvent(type="note").to_sse() == {"type": "note"}
    assert LearnEvent(type="state", state=FeynmanState.TEACH).to_sse() == {
        "type": "state",
        "state": "Teach",
    }


def test_to_sse_carries_loop_index_and_data():
    event = LearnEvent(
        type="gap", state=FeynmanState.RETURN, loop_index=3, data={"gap_type": "no_boundary"}
    )
    assert event.to_sse() == {
        "type": "gap",
        "state": "Return",
        "loop_index": 3,
        "data": {"gap_type": "no_boundary"},
    }


def test_to_sse_is_json_serializable_without_custom_encoder():
    """路由直接 ``json.dumps``: 载荷里不许混进 pydantic 模型或枚举对象。"""
    event = LearnEvent.of_gap(gap_item(), STATE)
    assert json.loads(json.dumps(event.to_sse(), ensure_ascii=False))["data"]["gap_type"] == (
        "causal_gap"
    )


def test_unknown_event_type_is_rejected():
    with pytest.raises(ValidationError):
        LearnEvent(type="bogus")  # type: ignore[arg-type]


def test_event_is_frozen_and_forbids_extras():
    event = LearnEvent(type="note", data={"text": "hi"})
    with pytest.raises(ValidationError):
        event.type = "state"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        LearnEvent(type="note", unexpected=1)  # type: ignore[call-arg]


# --------------------------------------------------------------------------- of_state


def test_of_state_reports_actions_persona_and_budgets():
    event = LearnEvent.of_state(
        STATE,
        actions=("probe", "supply_atoms"),
        reason="检出 2 个卡点",
        persona="first-principles-drill",
        probes_left=1,
        notes=("补给预算已压缩",),
    )
    payload = event.to_sse()
    assert payload["state"] == "Detect"
    assert payload["loop_index"] == 2
    data = payload["data"]
    assert data["concept_id"] == "闭包"
    assert data["actions"] == ["probe", "supply_atoms"]
    assert data["probes_left"] == 1
    assert data["reason"] == "检出 2 个卡点"
    assert data["persona"] == "first-principles-drill"
    assert data["notes"] == ["补给预算已压缩"]


def test_of_state_omits_optional_fields_when_absent():
    data = LearnEvent.of_state(STATE).to_sse()["data"]
    assert "reason" not in data
    assert "persona" not in data
    assert "notes" not in data
    assert "loop_capped" not in data
    assert "scores" not in data


def test_of_state_marks_loop_capped_only_when_true():
    capped = STATE.model_copy(update={"loop_capped": True})
    assert LearnEvent.of_state(capped).to_sse()["data"]["loop_capped"] is True


def test_of_state_rounds_talk_ratio_and_lists_gap_types():
    state = STATE.model_copy(
        update={
            "learner_chars": 1,
            "system_chars": 2,
            "open_gaps": (gap_item(), gap_item(type="no_boundary", severity=3)),
        }
    )
    data = LearnEvent.of_state(state).to_sse()["data"]
    assert data["talk_ratio"] == 0.333
    assert data["open_gaps"] == ["causal_gap", "no_boundary"]


def test_of_state_includes_last_scores_when_present():
    state = STATE.model_copy(
        update={"last_scores": GapScores(completeness=0.5, own_words=0.25, boundary_awareness=0.0)}
    )
    scores = LearnEvent.of_state(state).to_sse()["data"]["scores"]
    assert scores["completeness"] == 0.5
    assert scores["own_words"] == 0.25


# --------------------------------------------------------------------------- of_gap


def test_of_gap_carries_an_actionable_probe():
    """卡点必须是可执行的: ``probe`` 是「下一步该问什么」, 不是一句「理解不够」。"""
    data = LearnEvent.of_gap(gap_item(), STATE).to_sse()["data"]
    assert data["gap_type"] == "causal_gap"
    assert data["probe"] == "那一步为什么可以这么做?"
    assert data["severity"] == 2
    assert data["claim_id"] == "c1"


def test_of_gap_omits_claim_id_when_absent():
    data = LearnEvent.of_gap(gap_item(claim_id=""), STATE).to_sse()["data"]
    assert "claim_id" not in data


def test_of_gap_serializes_missing_edge_with_dag_aliases():
    """``MissingEdge`` 的字段是 ``from``/``to``(DAG 边的口径), 别名必须原样下发。"""
    item = gap_item(missing_edge=MissingEdge.model_validate({"from": "atom:a", "to": "atom:b"}))
    data = LearnEvent.of_gap(item, STATE).to_sse()["data"]
    assert data["missing_edge"] == {"from": "atom:a", "to": "atom:b"}


def test_of_gap_exposes_supply_budget_and_atom_ids():
    bundle = AtomBundle(
        atoms=(Atom(id="atom:a", statement="闭包捕获变量本身"), Atom(id="atom:b", statement="作用域")),
        max_chars=300,
    )
    data = LearnEvent.of_gap(gap_item(supply=bundle), STATE).to_sse()["data"]
    assert data["supply_atom_ids"] == ["atom:a", "atom:b"]
    assert data["supply_max_chars"] == 300


def test_of_gap_inherits_state_and_loop_index():
    payload = LearnEvent.of_gap(gap_item(), STATE).to_sse()
    assert payload["state"] == "Detect"
    assert payload["loop_index"] == 2


# --------------------------------------------------------------------------- of_guard


def test_of_guard_describes_guard_action_and_detail():
    data = LearnEvent.of_guard(violation(), STATE).to_sse()["data"]
    assert data["guard"] == "monologue_cap"
    assert data["action"] == "truncate"
    assert data["detail"] == "超了"


def test_of_guard_omits_replacement_when_nothing_was_rewritten():
    """没有 ``replacement`` = 内核没改写文本, 前端不该动气泡。"""
    assert "replacement" not in LearnEvent.of_guard(violation(), STATE).to_sse()["data"]


def test_of_guard_includes_replacement_even_when_empty():
    """改写结果是空串也要下发: 这与「没改写」是两件事(直答被整段拦截就是空)。"""
    data = LearnEvent.of_guard(violation(Guard.NO_DIRECT_ANSWER), STATE, replacement="").to_sse()[
        "data"
    ]
    assert data["replacement"] == ""
    assert data["guard"] == "no_direct_answer"


def test_of_guard_names_every_guard_in_vocabulary():
    for guard in Guard:
        data = LearnEvent.of_guard(violation(guard), STATE).to_sse()["data"]
        assert data["guard"] == guard.value


# --------------------------------------------------------------------------- of_attachment / of_mastery / of_note


def test_of_attachment_turns_overflow_into_a_card():
    data = LearnEvent.of_attachment(STATE, title="溢出", text="x" * 900).to_sse()["data"]
    assert data["title"] == "溢出"
    assert data["chars"] == 900
    assert len(data["text"]) == 900


def test_of_mastery_rounds_and_computes_delta():
    data = LearnEvent.of_mastery(STATE, atom_key="atom:a", before=0.42123, after=0.61891).to_sse()[
        "data"
    ]
    assert data["atom_key"] == "atom:a"
    assert data["before"] == 0.4212
    assert data["after"] == 0.6189
    assert data["delta"] == 0.1977


def test_of_mastery_omits_review_fields_until_scheduled():
    data = LearnEvent.of_mastery(STATE, atom_key="atom:a", before=0.1, after=0.2).to_sse()["data"]
    assert "review_in_days" not in data
    assert "review_stage" not in data


def test_of_mastery_includes_review_schedule_when_given():
    data = LearnEvent.of_mastery(
        STATE, atom_key="atom:a", before=0.1, after=0.5, review_in_days=1.0, review_stage=2
    ).to_sse()["data"]
    assert data["review_in_days"] == 1.0
    assert data["review_stage"] == 2


def test_of_note_keeps_text_and_tolerates_missing_state():
    assert LearnEvent.of_note("内核备注").to_sse() == {"type": "note", "data": {"text": "内核备注"}}
    payload = LearnEvent.of_note("内核备注", STATE).to_sse()
    assert payload["state"] == "Detect"
    assert payload["loop_index"] == 2


# --------------------------------------------------------------------------- PedagogySink


def test_sink_drain_returns_each_event_exactly_once():
    """``drain`` 是游标式的: 路由每次循环拉一把, 事件才能与 token 增量交错下发。"""
    sink = PedagogySink()
    sink.emit(LearnEvent(type="note", data={"text": "1"}))
    sink.emit(LearnEvent(type="note", data={"text": "2"}))
    assert [e.data["text"] for e in sink.drain()] == ["1", "2"]
    assert sink.drain() == []
    sink.emit(LearnEvent(type="note", data={"text": "3"}))
    assert [e.data["text"] for e in sink.drain()] == ["3"]
    assert sink.emitted == 3
    assert len(sink) == 3


def test_sink_extend_accepts_sequences():
    sink = PedagogySink()
    sink.extend((LearnEvent(type="note"), [LearnEvent(type="state")]))
    assert len(sink) == 2


def test_sink_is_truthy_even_when_empty():
    """空 sink 仍然为真: 它定义了 ``__len__``, 而教学刚开始时事件数恰好是 0。

    少了 ``__bool__``, 一个 ``if sink:`` 的笔误就会在最常见的那一刻(还没产出事件)
    静默关掉整个内核, 且不报任何错。判空必须写 ``sink is None``。
    """
    sink = PedagogySink()
    assert len(sink) == 0
    assert sink
    assert bool(sink) is True
    sink.emit(LearnEvent(type="note"))
    assert sink


def test_sink_mark_is_idempotent():
    """工具循环里模型会被调用多次, 记账/下发这类「只应发生一次」的事靠 mark 去重。"""
    sink = PedagogySink()
    assert sink.mark("accounted:abc") is True
    assert sink.mark("accounted:abc") is False
    assert sink.mark("accounted:def") is True


def test_sink_carrier_defaults_to_none_and_is_writable():
    sink = PedagogySink()
    assert sink.carrier is None
    carrier = LoopCarrier(state=STATE, turns=3)
    sink.carrier = carrier
    assert sink.carrier is carrier


# --------------------------------------------------------------------------- LoopCarrier


def test_carrier_channel_roundtrip():
    carrier = LoopCarrier(state=STATE, awaiting_escape=True, turns=4)
    restored = LoopCarrier.from_channel(carrier.to_channel())
    assert restored == carrier
    assert restored.state.open_gaps == ()
    assert restored.state.state is FeynmanState.DETECT


def test_carrier_channel_is_plain_json():
    """频道值要能被 checkpoint 的 JSON 序列化器写盘, 所以必须是原生 dict。"""
    payload = LoopCarrier(state=STATE, turns=1).to_channel()
    assert isinstance(payload, dict)
    assert json.loads(json.dumps(payload, ensure_ascii=False))["state"]["concept_id"] == "闭包"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "garbage",
        42,
        [],
        {},  # 缺 state 字段
        {"state": "not-a-state"},  # 状态名不在词表里
        {"state": {"thread_id": "t"}, "turns": -1},  # turns 有 ge=0 约束
        {"state": {"state": "Detect"}, "turns": "很多"},
    ],
)
def test_carrier_from_channel_is_tolerant(raw):
    """频道数据坏了当作没有(退回 ``None``, 从 Pick 重新开始), 绝不让 ``/api/chat`` 变 500。

    状态机是可重建的辅助信息, 账本在 MySQL(``learner_explanation`` / ``mastery_ledger``)。
    """
    assert LoopCarrier.from_channel(raw) is None


def test_carrier_from_channel_ignores_unknown_fields():
    """快照里多出来的键被忽略(而不是整体作废): 新版本写的频道, 旧代码仍能读出状态。"""
    carrier = LoopCarrier.from_channel({"state": {"unknown_field": 1}})
    assert carrier is not None
    assert carrier.state.state is FeynmanState.PICK


def test_carrier_from_channel_keeps_partial_state():
    carrier = LoopCarrier.from_channel({"state": {"state": "Return", "concept_id": "闭包"}, "turns": 3})
    assert carrier is not None
    assert carrier.turns == 3
    assert carrier.state.state is FeynmanState.RETURN
    assert carrier.state.concept_id == "闭包"


def test_carrier_is_frozen():
    carrier = LoopCarrier(state=STATE)
    with pytest.raises(ValidationError):
        carrier.turns = 5  # type: ignore[misc]


def test_carrier_defaults():
    carrier = LoopCarrier(state=STATE)
    assert carrier.awaiting_escape is False
    assert carrier.turns == 0
    assert STATE.terminal is False
    assert LoopCarrier(state=STATE.model_copy(update={"state": FeynmanState.CLOSED})).state.terminal
