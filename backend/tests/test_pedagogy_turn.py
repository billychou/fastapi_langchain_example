"""学习者侧判读(``app/pedagogy/turn.py``): 请求文本 -> 内核事件。

这是 roadmap P0 #6 的「学习者侧」一半: 命名概念、交出自述、请求直答三件事都从请求
文本直接判定, **全部是确定性正则, 不调 LLM**。两个理由:

- 概念名是掌握度账本的主键之一(``concept_id``), 让模型每次抽一个名字等于让账本碎裂;
- 「学习者交了一份自述」必须由服务端判读 —— 让模型自报等于让被考核的人自己填考勤表
  (中间件因此拒绝模型自报的学习者侧事件, 见 ``test_pedagogy_middleware.py``)。

覆盖三块: 概念抽取的边界(含刻意不认的写法)、slug 稳定性、``read_turn`` 的优先级链。
"""

from __future__ import annotations

import pytest
from app.pedagogy.policy import PedagogyConfig
from app.pedagogy.schemas import (
    ConceptNamed,
    EscapeRequested,
    ExplanationSubmitted,
    PedagogyState,
)
from app.pedagogy.states import ACTIVE_STATES, FeynmanState
from app.pedagogy.turn import (
    concept_slug,
    extract_concept,
    read_turn,
    wants_direct_answer,
    wants_to_learn,
)

CFG = PedagogyConfig()


def state_at(phase: FeynmanState, **overrides) -> PedagogyState:
    fields: dict = {
        "thread_id": "thread-turn",
        "account_id": 7,
        "learner_id": 7,
        "concept_id": "闭包",
        "state": phase,
        "loop_index": 1,
    }
    fields.update(overrides)
    return PedagogyState(**fields)


# --------------------------------------------------------------------------- 概念抽取


@pytest.mark.parametrize(
    ("text", "concept"),
    [
        ("请给我讲讲什么是闭包", "闭包"),
        ("给我讲讲闭包是什么", "闭包"),
        ("讲讲闭包", "闭包"),
        ("说说什么是闭包", "闭包"),
        ("讲解一下递归", "递归"),
    ],
)
def test_extract_concept_from_teach_phrasing(text: str, concept: str):
    """「讲讲 X」类句式: 疑问词与语气词都要被剥干净, 只留裸概念名。"""
    assert extract_concept(text) == concept


@pytest.mark.parametrize(
    ("text", "concept"),
    [
        ("我想搞懂闭包", "闭包"),
        ("帮我理解一下注意力机制", "注意力机制"),
        ("我要学会动态规划", "动态规划"),
        ("能不能讲解一下递归", "递归"),
        ("麻烦说说什么是熵增", "熵增"),
    ],
)
def test_extract_concept_from_learn_intent(text: str, concept: str):
    assert extract_concept(text) == concept


@pytest.mark.parametrize(
    ("text", "concept"),
    [
        ("检验我是不是真懂了闭包", "闭包"),
        ("测试一下我掌握了递归", "递归"),
        ("检查一下我是不是理解了熵增", "熵增"),
    ],
)
def test_extract_concept_from_check_request(text: str, concept: str):
    """「检验我是不是真懂了 X」也要认: 这是费曼循环最常见的开场白之一。"""
    assert extract_concept(text) == concept


def test_extract_concept_allows_single_char_after_question_lead():
    """「什么叫熵」->「熵」: 疑问前缀本身占 2-3 字, 不能让「至少 2 字」的门槛挡住它。"""
    assert extract_concept("讲一讲什么叫熵") == "熵"


@pytest.mark.parametrize(
    ("text", "concept"),
    [
        ("给我讲讲  什么是  闭包 ", "闭包"),
        ("请给我讲讲什么是「闭包」", "闭包"),
        ("我想搞懂《注意力机制》", "注意力机制"),
    ],
)
def test_extract_concept_ignores_whitespace_and_wrappers(text: str, concept: str):
    """多余空白与引号/书名号不进概念名: 同一节课不该因为标点记出两个 concept_id。"""
    assert extract_concept(text) == concept


@pytest.mark.parametrize(
    "text",
    [
        "你好",
        "闭包是什么？",  # 裸疑问句: 没有学习意图动词, 不认
        "教我熵",  # 「教我」+ 单字概念: 门槛 2 字, 宁可这一轮不认
        "考考我熵",  # 同上; 也绝不允许退回成「我熵」这种带人称的名字
        "我想搞懂《熵》",  # 剥掉书名号只剩 1 字, 同样不认
        "讲讲下",
        "",
        "   ",
    ],
)
def test_extract_concept_rejects_ambiguous_input(text: str):
    """认不出就返回 ``None``(路由据此走普通聊天), 绝不猜一个名字。"""
    assert extract_concept(text) is None


def test_extract_concept_keeps_qualified_names_intact():
    """限定语属于概念名的一部分: 剥掉就会把「transformer 里的注意力机制」记成通用概念。"""
    assert extract_concept("我想搞懂transformer里的注意力机制") == "transformer里的注意力机制"


@pytest.mark.parametrize(
    "text",
    ["", " ", "。。。", "？？？", "\n\t", "🎉" * 50, "x" * 5000, None, 123, ["闭包"]],
)
def test_extract_concept_never_raises(text):
    """脏输入只会得到 ``None``: 判读失败不该让 ``/api/chat`` 变 500。"""
    assert extract_concept(text) is None or isinstance(extract_concept(text), str)


# --------------------------------------------------------------------------- slug


def test_concept_slug_is_stable_and_normalized():
    assert concept_slug("闭包") == "闭包"
    assert concept_slug("Attention Mechanism") == "attention-mechanism"
    assert concept_slug("闭包") == concept_slug("闭包")  # 同一名字永远同一 slug


def test_concept_slug_drops_punctuation_and_caps_length():
    assert concept_slug("C++") == "c"
    assert concept_slug("a  b！c") == "a-bc"
    assert len(concept_slug("概" * 200)) == 64


def test_concept_slug_falls_back_when_empty():
    assert concept_slug("") == "concept"
    assert concept_slug("！！！") == "concept"


def test_concept_slug_distinguishes_concepts():
    assert concept_slug("闭包") != concept_slug("递归")


# --------------------------------------------------------------------------- 意图闸门


@pytest.mark.parametrize(
    ("text", "expected"),
    [("我想搞懂闭包", True), ("讲讲什么是熵", True), ("你好", False), ("直接告诉我答案", False)],
)
def test_wants_to_learn_gate(text: str, expected: bool):
    """路由用它决定「这一轮要不要下发教学状态」: 闲聊必须判 False, 否则每句话都被教学化。"""
    assert wants_to_learn(text) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("直接告诉我答案", True),
        ("别再问了", True),
        ("不用问了", True),
        ("我放弃", True),
        ("讲不动了", True),
        ("跳过吧", True),
        ("讲讲闭包", False),
        ("", False),
    ],
)
def test_wants_direct_answer(text: str, expected: bool):
    assert wants_direct_answer(text) is expected


# --------------------------------------------------------------------------- read_turn


def test_read_turn_empty_text_only_notes():
    reading = read_turn(state_at(FeynmanState.PICK), "", config=CFG)
    assert reading.event is None
    assert reading.intent == "none"
    assert reading.note


def test_read_turn_names_concept_in_pick():
    reading = read_turn(state_at(FeynmanState.PICK, concept_id=""), "我想搞懂闭包", config=CFG)
    assert isinstance(reading.event, ConceptNamed)
    assert reading.event.concept_id == "闭包"
    assert reading.event.title == "闭包"
    assert reading.intent == "start"
    assert reading.concept == "闭包"
    assert reading.awaiting_escape is False


def test_read_turn_pick_without_concept_explains_how_to_start():
    reading = read_turn(state_at(FeynmanState.PICK), "你好", config=CFG)
    assert reading.event is None
    assert "还没命名概念" in reading.note


def test_read_turn_teach_submits_explanation_with_char_count():
    text = "闭包就是函数带着它出生时的环境走" * 6
    reading = read_turn(state_at(FeynmanState.TEACH), text, config=CFG)
    assert isinstance(reading.event, ExplanationSubmitted)
    assert reading.event.chars == len(text)
    assert reading.intent == "explain"


def test_read_turn_teach_counts_even_a_short_utterance():
    """字数达不达标由内核判(``tick``), 判读层不猜: 一个字也算一次自述。"""
    reading = read_turn(state_at(FeynmanState.TEACH), "嗯", config=CFG)
    assert isinstance(reading.event, ExplanationSubmitted)
    assert reading.event.chars == 1


def test_read_turn_truncates_stored_text_but_keeps_full_char_count():
    """``chars`` 是全量字数(护栏口径), ``text`` 只是留档摘要, 不允许把整篇塞进事件。"""
    reading = read_turn(state_at(FeynmanState.TEACH), "x" * 5000, config=CFG)
    event = reading.event
    assert isinstance(event, ExplanationSubmitted)
    assert event.chars == 5000
    assert len(event.text) == CFG.min_explain_chars * 20


def test_read_turn_respects_injected_config():
    reading = read_turn(
        state_at(FeynmanState.TEACH), "闭包就是函数带着环境", config=PedagogyConfig(min_explain_chars=4)
    )
    event = reading.event
    assert isinstance(event, ExplanationSubmitted)
    assert len(event.text) == 10


@pytest.mark.parametrize("phase", [FeynmanState.DETECT, FeynmanState.RETURN, FeynmanState.SIMPLIFY])
def test_read_turn_needs_grader_outside_teach_and_pick(phase: FeynmanState):
    """``Detect``/``Return``/``Simplify`` 的推进需要教练侧事件(判分/补给/迁移题裁决)。"""
    reading = read_turn(state_at(phase), "我讲完了", config=CFG)
    assert reading.event is None
    assert reading.intent == "none"
    assert "判分器" in reading.note


def test_read_turn_escape_sets_awaiting_confirmation():
    reading = read_turn(state_at(FeynmanState.TEACH), "直接告诉我答案", config=CFG)
    assert isinstance(reading.event, EscapeRequested)
    assert reading.event.confirmed is False
    assert reading.intent == "escape"
    assert reading.awaiting_escape is True
    assert "escape_hatch" in reading.note


def test_read_turn_escape_outranks_explanation():
    """「我讲不动了, 直接告诉我」在 ``Teach`` 下是逃生舱, 不是一份自述。"""
    reading = read_turn(state_at(FeynmanState.TEACH), "我讲不动了，直接告诉我", config=CFG)
    assert isinstance(reading.event, EscapeRequested)
    assert reading.intent == "escape"


def test_read_turn_confirms_escape_only_after_being_asked():
    reading = read_turn(
        state_at(FeynmanState.TEACH), "是的", awaiting_escape=True, config=CFG
    )
    assert isinstance(reading.event, EscapeRequested)
    assert reading.event.confirmed is True
    assert reading.intent == "confirm_escape"
    assert reading.awaiting_escape is False


@pytest.mark.parametrize("phase", list(ACTIVE_STATES))
def test_read_turn_confirmation_works_in_every_active_state(phase: FeynmanState):
    """逃生舱在任意非终态都可确认(``TRANSITIONS`` 里每个 ACTIVE 状态都有这条边)。"""
    reading = read_turn(state_at(phase), "嗯，确认", awaiting_escape=True, config=CFG)
    assert isinstance(reading.event, EscapeRequested)
    assert reading.event.confirmed is True


def test_read_turn_bare_yes_without_prior_ask_is_not_an_escape():
    """没问过就直接说「是的」不构成直答请求: 否则一句附和就能拿到答案。"""
    reading = read_turn(state_at(FeynmanState.PICK), "是的", config=CFG)
    assert not isinstance(reading.event, EscapeRequested)
    assert reading.intent == "none"


def test_read_turn_denial_withdraws_escape():
    reading = read_turn(
        state_at(FeynmanState.TEACH), "不用了，我还是自己讲", awaiting_escape=True, config=CFG
    )
    assert reading.event is None
    assert reading.intent == "deny_escape"
    assert reading.awaiting_escape is False
    assert "撤回" in reading.note


def test_read_turn_denial_beats_confirmation():
    """否定优先: 「不用了」里同时含「用」这类肯定信号时, 必须按撤回处理。"""
    reading = read_turn(state_at(FeynmanState.TEACH), "算了，不用了", awaiting_escape=True, config=CFG)
    assert reading.intent == "deny_escape"
    assert reading.event is None


def test_read_turn_pending_confirmation_survives_an_unrelated_reply():
    """学习者答非所问时, 「已经问过了」这个跨轮事实不能被清掉。"""
    reading = read_turn(
        state_at(FeynmanState.TEACH), "闭包就是函数带着环境", awaiting_escape=True, config=CFG
    )
    assert isinstance(reading.event, ExplanationSubmitted)
    assert reading.awaiting_escape is True


@pytest.mark.parametrize(
    ("phase", "text", "awaiting"),
    [
        (FeynmanState.PICK, "", False),
        (FeynmanState.PICK, "🎉" * 30, True),
        (FeynmanState.TEACH, "x" * 3000, False),
        (FeynmanState.DETECT, "？？？", True),
        (FeynmanState.CLOSED, "我想搞懂递归", False),
    ],
)
def test_read_turn_never_raises(phase: FeynmanState, text: str, awaiting: bool):
    reading = read_turn(state_at(phase), text, awaiting_escape=awaiting, config=CFG)
    assert reading.intent in {
        "start",
        "explain",
        "escape",
        "confirm_escape",
        "deny_escape",
        "none",
    }


@pytest.mark.parametrize("phase", list(ACTIVE_STATES))
def test_read_turn_events_never_self_report_system_chars(phase: FeynmanState):
    """``system_chars`` 一律为 0: 系统说了多少字由中间件按 ``len(text)`` 记一次。

    学习者侧事件若自带字数, ``learner_talk_ratio`` 护栏就会被重复记账带偏。
    """
    for text in ("我想搞懂闭包", "闭包就是函数带着环境" * 8, "直接告诉我", "是的"):
        reading = read_turn(state_at(phase), text, awaiting_escape=True, config=CFG)
        if reading.event is not None:
            assert reading.event.system_chars == 0
