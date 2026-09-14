"""路由侧装配(``app/pedagogy/session.py``): 一轮 HTTP 请求 -> :class:`PedagogyTurn`。

这一层是「什么时候才教学」的闸门, 覆盖两类断言:

- **不该教学时绝不打扰**: 降级开关、匿名、闲聊、终态旧循环四种情况都必须返回 ``None``,
  普通聊天的行为与注入内核之前逐字一致(见 ``test_chat_api.py``);
- **该教学时状态先行**: 装配在调模型**之前**完成, 于是中间件能把新状态注入系统提示,
  ``learn`` 事件也能与 token 增量交错下发, 而不是等流结束才补一张状态卡。

模块本身不 ``await``、不查库、不碰 agent, 所以这里可以穷举。
"""

from __future__ import annotations

import json

import pytest
from app.context import ChatContext
from app.pedagogy.events import LoopCarrier
from app.pedagogy.policy import PedagogyConfig
from app.pedagogy.schemas import PedagogyState
from app.pedagogy.session import PedagogyTurn, prepare_turn
from app.pedagogy.states import ACTIVE_STATES, FeynmanState

CFG = PedagogyConfig()
THREAD = "thread-session"
ACCOUNT = 7
LONG_EXPLANATION = "闭包就是函数记住并访问它被创建时的词法作用域，即使函数在别处被调用也照样能读到那些变量。" * 2


def channel(
    phase: FeynmanState = FeynmanState.TEACH,
    *,
    concept_id: str = "闭包",
    loop_index: int = 1,
    turns: int = 2,
    awaiting_escape: bool = False,
    learner_chars: int = 0,
    system_chars: int = 0,
) -> dict:
    """一个已存在于 checkpointer ``pedagogy`` 频道里的载体(JSON 字典)。"""
    state = PedagogyState(
        thread_id=THREAD,
        account_id=ACCOUNT,
        learner_id=ACCOUNT,
        concept_id=concept_id,
        state=phase,
        loop_index=loop_index,
        learner_chars=learner_chars,
        system_chars=system_chars,
    )
    return LoopCarrier(state=state, awaiting_escape=awaiting_escape, turns=turns).to_channel()


def prepare(text: str, *, raw_channel=None, config: PedagogyConfig | None = None, **kwargs):
    return prepare_turn(
        raw_channel=raw_channel,
        thread_id=kwargs.pop("thread_id", THREAD),
        account_id=kwargs.pop("account_id", ACCOUNT),
        text=text,
        config=config or CFG,
        **kwargs,
    )


# --------------------------------------------------------------------------- 闸门: 何时不教学


def test_disabled_flag_turns_pedagogy_off():
    """``PEDAGOGY_ENABLED=false`` 是全局降级开关: 内核出问题时聊天必须还能用。"""
    assert prepare("我想搞懂闭包", config=PedagogyConfig(enabled=False)) is None


@pytest.mark.parametrize("account_id", [0, -1])
def test_anonymous_request_is_not_tutored(account_id: int):
    """匿名没有证据归属(``learner_explanation`` 需要 account_id), 教学技能也是 auth 级。"""
    assert prepare("我想搞懂闭包", account_id=account_id) is None


@pytest.mark.parametrize("text", ["你好", "现在几点了", "", "   "])
def test_chitchat_without_prior_loop_is_not_tutored(text: str):
    """这个线程上没跑过循环、本轮也没有学习意图 -> 闲聊就是闲聊。"""
    assert prepare(text) is None


def test_terminal_loop_without_new_concept_is_not_resumed():
    """上一轮已 ``Closed``: 不拿旧循环去套新问题(旧快照的字数与卡点属于上一个概念)。"""
    assert prepare("你好", raw_channel=channel(FeynmanState.CLOSED)) is None
    assert prepare("继续", raw_channel=channel(FeynmanState.ESCAPED)) is None


def test_corrupt_channel_without_intent_is_not_tutored():
    assert prepare("你好", raw_channel="garbage") is None


# --------------------------------------------------------------------------- 新循环


def test_fresh_loop_starts_at_teach_with_named_concept():
    turn = prepare("我想搞懂闭包")
    assert turn is not None
    assert turn.state.state is FeynmanState.TEACH
    assert turn.state.concept_id == "闭包"
    assert turn.state.loop_index == 1
    assert turn.carrier.turns == 1
    assert turn.carrier.awaiting_escape is False
    assert turn.step is not None


def test_fresh_loop_carries_request_identity():
    turn = prepare("我想搞懂熵增", thread_id="thread-xyz", account_id=42)
    assert turn is not None
    assert turn.state.thread_id == "thread-xyz"
    assert turn.state.account_id == 42
    assert turn.state.learner_id == 42


def test_fresh_loop_emits_state_card_before_model_runs():
    """``pre_events`` 的第一条必须是状态卡: 学习者先看到「现在在哪一步」。"""
    turn = prepare("我想搞懂闭包")
    assert turn is not None
    first = turn.pre_events[0]
    assert first.type == "state"
    assert first.state is FeynmanState.TEACH
    assert first.loop_index == 1
    data = first.to_sse()["data"]
    assert data["actions"] == ["ask_explain"]
    assert data["persona"] == "feynman-tutor"
    assert data["probes_left"] == CFG.max_probes_per_turn
    assert data["concept_id"] == "闭包"
    assert data["reason"]


def test_terminal_loop_with_new_concept_restarts_from_scratch():
    turn = prepare("我想搞懂递归", raw_channel=channel(FeynmanState.CLOSED, turns=5))
    assert turn is not None
    assert turn.state.state is FeynmanState.TEACH
    assert turn.state.concept_id == "递归"
    assert turn.state.loop_index == 1
    assert turn.state.learner_chars == 0
    assert turn.state.open_gaps == ()
    # turns 是线程级可观测计数, 跨概念继续累加(它不参与裁决)
    assert turn.carrier.turns == 6


def test_corrupt_channel_restarts_instead_of_failing():
    turn = prepare("我想搞懂闭包", raw_channel={"state": "not-a-state"})
    assert turn is not None
    assert turn.state.state is FeynmanState.TEACH
    assert turn.state.thread_id == THREAD


# --------------------------------------------------------------------------- 续跑


def test_resume_in_teach_with_enough_chars_enters_detect():
    turn = prepare(LONG_EXPLANATION, raw_channel=channel(FeynmanState.TEACH))
    assert turn is not None
    assert turn.state.state is FeynmanState.DETECT
    assert turn.state.learner_chars == len(LONG_EXPLANATION)
    assert turn.pre_events[0].to_sse()["data"]["actions"] == ["probe"]


def test_resume_in_teach_with_short_reply_stays_and_asks_for_more():
    turn = prepare("嗯", raw_channel=channel(FeynmanState.TEACH))
    assert turn is not None
    assert turn.state.state is FeynmanState.TEACH
    data = turn.pre_events[0].to_sse()["data"]
    assert data["actions"] == ["ask_more_detail"]
    assert "min_explain_chars" in data["reason"]


def test_resume_honours_injected_thresholds():
    turn = prepare(
        "闭包就是函数带着环境",
        raw_channel=channel(FeynmanState.TEACH),
        config=PedagogyConfig(min_explain_chars=4),
    )
    assert turn is not None
    assert turn.state.state is FeynmanState.DETECT


def test_resume_accumulates_turn_counter():
    turn = prepare("嗯", raw_channel=channel(FeynmanState.TEACH, turns=9))
    assert turn is not None
    assert turn.carrier.turns == 10


@pytest.mark.parametrize("phase", [FeynmanState.DETECT, FeynmanState.RETURN, FeynmanState.SIMPLIFY])
def test_resume_in_grading_states_keeps_state_and_explains_why(phase: FeynmanState):
    """这些状态的推进需要教练侧事件(判分/补给/迁移题裁决), 学习者侧只能给一句说明。"""
    turn = prepare("我讲完了", raw_channel=channel(phase))
    assert turn is not None
    assert turn.state.state is phase
    assert turn.step is None
    assert len(turn.pre_events) == 1
    assert turn.pre_events[0].type == "note"
    assert "判分器" in turn.pre_events[0].data["text"]


def test_idle_pick_turn_only_emits_a_hint():
    turn = prepare("你好", raw_channel=channel(FeynmanState.PICK))
    assert turn is not None
    assert turn.state.state is FeynmanState.PICK
    assert turn.step is None
    assert [e.type for e in turn.pre_events] == ["note"]


# --------------------------------------------------------------------------- 逃生舱


def test_escape_request_sets_awaiting_and_emits_guard():
    turn = prepare("直接告诉我答案", raw_channel=channel(FeynmanState.TEACH))
    assert turn is not None
    assert turn.state.state is FeynmanState.TEACH  # 未确认前不真的直答
    assert turn.carrier.awaiting_escape is True
    assert [e.type for e in turn.pre_events] == ["state", "guard", "note"]
    assert turn.pre_events[0].to_sse()["data"]["actions"] == ["confirm_escape"]
    assert turn.pre_events[1].to_sse()["data"]["guard"] == "escape_hatch"


def test_pre_events_order_is_state_then_gap_then_guard_then_note():
    """顺序固定: 先「在哪一步」, 再「哪里断了」, 最后才是内核对他这句话的解释。"""
    turn = prepare("直接告诉我答案", raw_channel=channel(FeynmanState.RETURN))
    assert turn is not None
    types = [event.type for event in turn.pre_events]
    assert types[0] == "state"
    assert types.index("guard") < types.index("note")


def test_soft_guard_is_deferred_to_the_middleware():
    """``learner_talk_ratio`` 是软护栏(``action=log``): 这里不发, 中间件在模型说完之后发。

    装配时 ``system_chars`` 还是**上一轮**的累计值(本轮回复一个字都还没说), 两边都发
    就会让学习者在同一轮里看到两张只有数字不同的「你说得太少」卡片。走真实 HTTP 才
    暴露得出来(见 ``tests/test_chat_pedagogy_api.py``): 单测里 ``system_chars`` 默认 0,
    占比恒为 1.0, 根本不触发。
    """
    turn = prepare(LONG_EXPLANATION, raw_channel=channel(FeynmanState.TEACH, system_chars=500))
    assert turn is not None
    assert turn.step is not None
    # 裁决信息没丢: 落库/看板仍从 step.violations 拿全量, 只是不提前下发
    assert [item.guard.value for item in turn.step.violations] == ["learner_talk_ratio"]
    assert [event.type for event in turn.pre_events] == ["state"]


def test_hard_guards_still_ship_before_the_model_runs():
    """硬护栏描述的是学习者**这句话**触发了什么, 必须立刻可见, 不能等模型说完。"""
    turn = prepare("直接告诉我答案", raw_channel=channel(FeynmanState.TEACH, system_chars=500))
    assert turn is not None
    shipped = [e.to_sse()["data"]["guard"] for e in turn.pre_events if e.type == "guard"]
    assert shipped == ["escape_hatch"]


def test_confirmed_escape_ends_loop_without_mastery_credit():
    turn = prepare("是的", raw_channel=channel(FeynmanState.TEACH, awaiting_escape=True))
    assert turn is not None
    assert turn.state.state is FeynmanState.ESCAPED
    assert turn.carrier.awaiting_escape is False
    data = turn.pre_events[0].to_sse()["data"]
    assert data["actions"] == ["no_mastery_credit"]
    assert any("不计入掌握度" in note for note in data["notes"])


@pytest.mark.parametrize("phase", list(ACTIVE_STATES))
def test_escape_can_be_requested_from_any_active_state(phase: FeynmanState):
    turn = prepare("我放弃了，直接告诉我", raw_channel=channel(phase))
    assert turn is not None
    assert turn.carrier.awaiting_escape is True


def test_denied_escape_clears_the_pending_flag():
    turn = prepare(
        "不用了，我还是自己讲", raw_channel=channel(FeynmanState.TEACH, awaiting_escape=True)
    )
    assert turn is not None
    assert turn.carrier.awaiting_escape is False
    assert turn.state.state is FeynmanState.TEACH


# --------------------------------------------------------------------------- 交接给 context / sink


def test_context_kwargs_are_exactly_the_pedagogy_fields():
    """``ChatContext(**身份字段, **turn.context_kwargs)`` 直接展开, 键名不许漂。"""
    turn = prepare("我想搞懂闭包")
    assert turn is not None
    assert set(turn.context_kwargs) == {
        "learner_id",
        "pedagogy_state",
        "pedagogy_step",
        "pedagogy_sink",
    }


def test_context_kwargs_build_a_valid_chat_context():
    turn = prepare("我想搞懂闭包")
    assert turn is not None
    context = ChatContext(
        account_id=ACCOUNT, authenticated=True, permissions=frozenset({"*"}), **turn.context_kwargs
    )
    assert context.pedagogy_state is turn.state
    assert context.pedagogy_sink is turn.sink
    assert context.pedagogy_step is turn.step
    assert context.learner_id == ACCOUNT


def test_sink_is_preloaded_with_the_turn_carrier():
    """中间件续写的是同一个载体: 它不能持有请求态(agent 是进程级单例)。"""
    turn = prepare("我想搞懂闭包")
    assert turn is not None
    assert turn.sink.carrier is turn.carrier
    assert turn.sink.emitted == 0  # pre_events 由路由下发, 不进 sink
    assert isinstance(turn, PedagogyTurn)


def test_turn_state_mirrors_carrier():
    turn = prepare(LONG_EXPLANATION, raw_channel=channel(FeynmanState.TEACH))
    assert turn is not None
    assert turn.state is turn.carrier.state


def test_prepare_turn_does_not_mutate_the_incoming_channel():
    """频道字典是 checkpointer 读出来的快照: 装配只读不写(写回由中间件负责)。"""
    raw = channel(FeynmanState.TEACH)
    snapshot = _dump(raw)
    prepare(LONG_EXPLANATION, raw_channel=raw)
    assert _dump(raw) == snapshot


def _dump(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)
