"""费曼循环状态机 + 护栏的单元测试(不经 LLM、不碰数据库)。

对应 ``docs/education-agent-roadmap.md`` P0 交付物 #1 与 ``docs/pedagogy-core.md`` §8
「状态机转移: 直接调用 ``tick(event)``, 不经 LLM」。
"""

from __future__ import annotations

import pytest
from app.config import Settings
from app.pedagogy import (
    TERMINAL_STATES,
    TRANSITIONS,
    Action,
    Atom,
    AtomBundle,
    Claim,
    ConceptNamed,
    EscapeRequested,
    ExplanationSubmitted,
    ExplanationTurn,
    FeynmanState,
    GapItem,
    GapReport,
    GapReported,
    GapScores,
    Guard,
    GuardAction,
    LoopStep,
    MissingEdge,
    PedagogyConfig,
    PedagogyState,
    SimplifyJudged,
    SupplyCompleted,
    closure_verdict,
    tick,
)
from app.pedagogy.policy import (
    DIRECT_ANSWER_REWRITE_HINT,
    check_talk_ratio,
    enforce_monologue_cap,
    enforce_no_direct_answer,
    enforce_probe_budget,
    escape_violation,
    find_solution_start,
    talk_ratio,
)
from app.pedagogy.states import ACTIVE_STATES, allowed_events, can

CFG = PedagogyConfig()  # 默认阈值即 pedagogy-core 写明的数字


def gap(probe: str = "为什么可以这样?", gap_type: str = "causal_gap", severity: int = 1) -> GapItem:
    return GapItem(
        type=gap_type,  # type: ignore[arg-type]
        claim_id="c1",
        probe=probe,
        severity=severity,
        supply=AtomBundle(atoms=(Atom(id="atom:a", statement="A"),), max_chars=300),
        missing_edge=MissingEdge.model_validate({"from": "atom:a", "to": "atom:b"}),
    )


def report(
    *,
    gaps: tuple[GapItem, ...] = (),
    verdict: str = "not_closed",
    completeness: float = 0.9,
    own_words: float = 0.9,
    wrong: tuple[str, ...] = (),
) -> GapReport:
    return GapReport(
        gaps=gaps,
        verdict=verdict,  # type: ignore[arg-type]
        scores=GapScores(completeness=completeness, own_words=own_words, reason="测试"),
        wrong_claim_ids=wrong,
    )


def in_state(phase: FeynmanState, **kwargs) -> PedagogyState:
    return PedagogyState(state=phase, concept_id="scaled-dot-product", **kwargs)


# --------------------------------------------------------------------------- 转移表


def test_transition_table_covers_seven_states():
    assert len(FeynmanState) == 7
    assert TERMINAL_STATES == frozenset({FeynmanState.CLOSED, FeynmanState.ESCAPED})
    assert len(ACTIVE_STATES) == 5


def test_terminal_states_accept_no_events():
    for state in TERMINAL_STATES:
        assert allowed_events(state) == ()


def test_every_active_state_can_request_escape():
    """逃生舱在任意非终态都可用(pedagogy-core §1.2)。"""
    for state in ACTIVE_STATES:
        assert can(state, "escape_requested")
        assert FeynmanState.ESCAPED in TRANSITIONS[(state, "escape_requested")]


def test_happy_path_edges_exist():
    expected = {
        (FeynmanState.PICK, "concept_named"): FeynmanState.TEACH,
        (FeynmanState.TEACH, "explanation_submitted"): FeynmanState.DETECT,
        (FeynmanState.DETECT, "gap_reported"): FeynmanState.RETURN,
        (FeynmanState.RETURN, "supply_completed"): FeynmanState.TEACH,
        (FeynmanState.SIMPLIFY, "simplify_judged"): FeynmanState.CLOSED,
    }
    for key, target in expected.items():
        assert target in TRANSITIONS[key], key


# --------------------------------------------------------------------------- 逐条转移


def test_pick_names_concept_and_enters_first_loop():
    step = tick(
        PedagogyState(),
        ConceptNamed(concept_id="attention", prerequisites=("atom:dot-product",)),
        config=CFG,
    )
    assert step.phase is FeynmanState.TEACH
    assert step.state.concept_id == "attention"
    assert step.state.prerequisites == ("atom:dot-product",)
    assert step.state.loop_index == 1
    assert step.actions == (Action.ASK_EXPLAIN,)
    assert not step.terminal


def test_short_explanation_stays_in_teach():
    """pedagogy-core §8 的示例断言: 30 字自述 -> 仍停在 ``Teach``。"""
    step = tick(
        in_state(FeynmanState.TEACH, loop_index=1),
        ExplanationSubmitted(chars=30, text="就是点积然后 softmax"),
        config=CFG,
    )
    assert step.phase is FeynmanState.TEACH
    assert step.actions == (Action.ASK_MORE_DETAIL,)
    assert step.state.learner_chars == 30


def test_long_enough_explanation_enters_detect():
    step = tick(
        in_state(FeynmanState.TEACH, loop_index=1),
        ExplanationSubmitted(chars=CFG.min_explain_chars, text="x" * 80),
        config=CFG,
    )
    assert step.phase is FeynmanState.DETECT
    assert step.actions == (Action.PROBE,)
    assert step.state.probes_used == 0


def test_detect_returns_when_gaps_are_open():
    step = tick(
        in_state(FeynmanState.DETECT, loop_index=1),
        GapReported(report=report(gaps=(gap(),))),
        config=CFG,
    )
    assert step.phase is FeynmanState.RETURN
    assert step.actions == (Action.SUPPLY_ATOMS,)
    assert len(step.state.open_gaps) == 1
    assert step.state.probes_used == 1


def test_detect_simplifies_when_nothing_is_open():
    for event in (
        GapReported(report=report(gaps=(), verdict="closed")),
        GapReported(report=report(gaps=())),  # not_closed 但没有卡点 -> 没什么可补给
    ):
        step = tick(in_state(FeynmanState.DETECT, loop_index=1), event, config=CFG)
        assert step.phase is FeynmanState.SIMPLIFY
        assert step.actions == (Action.REQUEST_SIMPLIFY,)
        assert step.state.open_gaps == ()


def test_supply_completed_asks_for_re_explanation():
    step = tick(
        in_state(FeynmanState.RETURN, loop_index=1, open_gaps=(gap(),)),
        SupplyCompleted(bundle=AtomBundle(atoms=(Atom(id="atom:a", statement="A"),))),
        config=CFG,
    )
    assert step.phase is FeynmanState.TEACH
    assert step.state.loop_index == 2
    assert step.state.open_gaps == ()
    assert step.actions == (Action.ASK_EXPLAIN,)


def test_simplify_closes_when_rubric_and_transfer_pass():
    step = tick(
        in_state(FeynmanState.SIMPLIFY, loop_index=1),
        SimplifyJudged(report=report(verdict="closed"), transfer_passed=True),
        config=CFG,
    )
    assert step.phase is FeynmanState.CLOSED
    assert step.terminal
    assert step.actions == (Action.RECORD_MASTERY, Action.SCHEDULE_REVIEW)


def test_simplify_reopens_teach_when_transfer_fails():
    step = tick(
        in_state(FeynmanState.SIMPLIFY, loop_index=1),
        SimplifyJudged(report=report(verdict="closed"), transfer_passed=False),
        config=CFG,
    )
    assert step.phase is FeynmanState.TEACH
    assert step.state.loop_index == 2
    assert not step.state.loop_capped
    assert any("迁移题未通过" in note for note in step.notes)


def test_kernel_rejects_a_claimed_pass_that_misses_the_rubric():
    """判分器说 closed 也不算: 阈值由内核裁决(pedagogy-core §3)。"""
    step = tick(
        in_state(FeynmanState.SIMPLIFY, loop_index=1),
        SimplifyJudged(
            report=report(verdict="closed", completeness=0.5, own_words=0.9),
            transfer_passed=True,
        ),
        config=CFG,
    )
    assert step.phase is FeynmanState.TEACH
    assert any("completeness" in note for note in step.notes)


def test_jargon_shield_blocks_closure_even_with_good_scores():
    step = tick(
        in_state(FeynmanState.SIMPLIFY, loop_index=1),
        SimplifyJudged(
            report=report(verdict="closed", gaps=(gap(gap_type="jargon_shield"),)),
            transfer_passed=True,
        ),
        config=CFG,
    )
    assert step.phase is not FeynmanState.CLOSED
    assert any("jargon_shield" in note for note in step.notes)


def test_wrong_claim_blocks_closure():
    step = tick(
        in_state(FeynmanState.SIMPLIFY, loop_index=1),
        SimplifyJudged(report=report(verdict="closed", wrong=("c3",)), transfer_passed=True),
        config=CFG,
    )
    assert step.phase is not FeynmanState.CLOSED
    assert any("wrong" in note for note in step.notes)


# --------------------------------------------------------------------------- 循环上限


def test_simplify_at_loop_cap_escalates_instead_of_reopening():
    step = tick(
        in_state(FeynmanState.SIMPLIFY, loop_index=CFG.max_loops),
        SimplifyJudged(report=report(), transfer_passed=False),
        config=CFG,
    )
    assert step.phase is FeynmanState.SIMPLIFY
    assert step.state.loop_capped is True
    assert step.actions == (Action.ESCALATE,)


def test_return_at_loop_cap_escalates_instead_of_re_explaining():
    step = tick(
        in_state(FeynmanState.RETURN, loop_index=CFG.max_loops),
        SupplyCompleted(),
        config=CFG,
    )
    assert step.phase is FeynmanState.RETURN
    assert step.state.loop_capped is True
    assert step.actions == (Action.ESCALATE,)


def test_loop_index_never_exceeds_max_loops_over_a_long_session():
    """穷举式回归: 无论怎么走, 进入 Teach 的次数不超过 max_loops。"""
    state = PedagogyState()
    state = tick(state, ConceptNamed(concept_id="c"), config=CFG).state
    for _ in range(10):
        state = tick(state, ExplanationSubmitted(chars=200), config=CFG).state
        state = tick(state, GapReported(report=report(gaps=(gap(),))), config=CFG).state
        state = tick(state, SupplyCompleted(), config=CFG).state
    assert state.loop_index <= CFG.max_loops
    assert state.loop_capped is True


# --------------------------------------------------------------------------- 逃生舱


def test_unconfirmed_escape_requires_confirmation():
    before = in_state(FeynmanState.TEACH, loop_index=1)
    step = tick(before, EscapeRequested(confirmed=False), config=CFG)
    assert step.phase is FeynmanState.TEACH  # 留在原状态
    assert step.actions == (Action.CONFIRM_ESCAPE,)
    assert step.violations[0].guard is Guard.ESCAPE_HATCH
    assert step.violations[0].action is GuardAction.CONFIRM


def test_confirmed_escape_ends_loop_without_mastery_credit():
    step = tick(in_state(FeynmanState.DETECT, loop_index=1), EscapeRequested(confirmed=True), config=CFG)
    assert step.phase is FeynmanState.ESCAPED
    assert step.terminal
    assert step.actions == (Action.NO_MASTERY_CREDIT,)
    assert Action.RECORD_MASTERY not in step.actions


def test_escape_preserves_open_gaps_as_evidence():
    """「在哪里放弃的」本身是学习证据, 不能被清掉。"""
    step = tick(
        in_state(FeynmanState.RETURN, loop_index=1, open_gaps=(gap(),)),
        EscapeRequested(confirmed=True),
        config=CFG,
    )
    assert step.phase is FeynmanState.ESCAPED
    assert len(step.state.open_gaps) == 1


def test_escape_violation_helper():
    assert escape_violation(confirmed=True) is None
    assert escape_violation(confirmed=False).guard is Guard.ESCAPE_HATCH


# --------------------------------------------------------------------------- 非法/终态事件


def test_terminal_state_ignores_events():
    closed = in_state(FeynmanState.CLOSED, loop_index=1)
    step = tick(closed, ExplanationSubmitted(chars=500), config=CFG)
    assert step.state is closed
    assert step.actions == ()
    assert "终态" in step.reason


def test_illegal_event_is_a_no_op_not_an_exception():
    """模型输出的不确定性不应该让 /api/chat 变成 500。"""
    teaching = in_state(FeynmanState.TEACH, loop_index=1)
    step = tick(teaching, SupplyCompleted(), config=CFG)
    assert step.state is teaching
    assert step.actions == ()
    assert "不接受" in step.reason


def test_illegal_events_never_raise_for_any_state_event_pair():
    events = [
        ConceptNamed(concept_id="c"),
        ExplanationSubmitted(chars=200),
        GapReported(report=report()),
        SupplyCompleted(),
        SimplifyJudged(report=report(), transfer_passed=True),
        EscapeRequested(confirmed=True),
    ]
    for phase in FeynmanState:
        for event in events:
            step = tick(PedagogyState(state=phase), event, config=CFG)
            assert isinstance(step, LoopStep)


# --------------------------------------------------------------------------- 护栏: 发言占比


def test_talk_ratio_math():
    assert talk_ratio(0, 0) == 0.0
    assert talk_ratio(60, 40) == pytest.approx(0.6)
    assert talk_ratio(30, 70) == pytest.approx(0.3)


def test_low_talk_ratio_is_logged_but_does_not_block():
    violation = check_talk_ratio(30, 70, CFG)
    assert violation is not None
    assert violation.guard is Guard.LEARNER_TALK_RATIO
    assert violation.action is GuardAction.LOG  # 软护栏: 只记录


def test_healthy_talk_ratio_has_no_violation():
    assert check_talk_ratio(60, 40, CFG) is None


def test_entering_detect_with_a_teacher_heavy_loop_flags_the_ratio():
    step = tick(
        in_state(FeynmanState.TEACH, loop_index=1, system_chars=400),
        ExplanationSubmitted(chars=100),
        config=CFG,
    )
    assert step.phase is FeynmanState.DETECT  # 不阻断转移
    assert [v.guard for v in step.violations] == [Guard.LEARNER_TALK_RATIO]
    assert step.state.talk_ratio == pytest.approx(0.2)


# --------------------------------------------------------------------------- 护栏: 不直答


def test_find_solution_start_needs_enough_steps():
    assert find_solution_start("1. 第一步\n2. 第二步\n", CFG.min_solution_steps) is None
    text = "1. 求导\n2. 令导数为零\n3. 代回\n"
    assert find_solution_start(text, CFG.min_solution_steps) == 0


def test_find_solution_start_catches_chinese_step_markers():
    text = "先看一下背景。\n第 1 步：列方程\n第 2 步：化简\n第 3 步：求解\n"
    assert find_solution_start(text, CFG.min_solution_steps) is not None


def test_find_solution_start_catches_bare_answers():
    assert find_solution_start("答案是 42", CFG.min_solution_steps) == 0
    assert find_solution_start("最终结果：42", CFG.min_solution_steps) == 0


def test_no_direct_answer_truncates_in_detect():
    text = "我们来看看这道题。\n1. 求导\n2. 令导数为零\n3. 代回得到极值\n"
    kept, violation = enforce_no_direct_answer(FeynmanState.DETECT, text, CFG)
    assert violation is not None
    assert violation.guard is Guard.NO_DIRECT_ANSWER
    assert violation.action is GuardAction.TRUNCATE
    assert "1. 求导" not in kept
    assert kept.endswith(DIRECT_ANSWER_REWRITE_HINT)


def test_no_direct_answer_applies_in_return_too():
    _, violation = enforce_no_direct_answer(FeynmanState.RETURN, "答案：42", CFG)
    assert violation is not None


@pytest.mark.parametrize(
    "phase", [FeynmanState.PICK, FeynmanState.TEACH, FeynmanState.SIMPLIFY, FeynmanState.CLOSED]
)
def test_no_direct_answer_leaves_other_states_alone(phase):
    text = "1. 求导\n2. 令导数为零\n3. 代回\n"
    kept, violation = enforce_no_direct_answer(phase, text, CFG)
    assert kept == text
    assert violation is None


def test_no_direct_answer_keeps_clean_text_untouched():
    text = "你说的『缩放』是指什么？缺的是哪一条 Atom？"
    kept, violation = enforce_no_direct_answer(FeynmanState.DETECT, text, CFG)
    assert (kept, violation) == (text, None)


# --------------------------------------------------------------------------- 护栏: 追问预算


def test_probe_budget_suppresses_the_fourth_probe():
    kept, violation = enforce_probe_budget(["a", "b", "c", "d", "e"], CFG)
    assert kept == ("a", "b", "c")
    assert violation.guard is Guard.PROBE_BUDGET
    assert violation.action is GuardAction.SUPPRESS
    assert "2" in violation.detail  # 抑制了 2 个


def test_probe_budget_within_limit_has_no_violation():
    kept, violation = enforce_probe_budget(["a", "b"], CFG)
    assert (kept, violation) == (("a", "b"), None)


def test_gap_report_keeps_only_budgeted_probes_and_orders_by_severity():
    gaps = (
        gap(probe="低优先级", severity=1),
        gap(probe="最高优先级", severity=3),
        gap(probe="中优先级", severity=2),
        gap(probe="第四个", severity=1),
    )
    step = tick(
        in_state(FeynmanState.DETECT, loop_index=1),
        GapReported(report=report(gaps=gaps)),
        config=CFG,
    )
    probes = [item.probe for item in step.state.open_gaps]
    assert probes == ["最高优先级", "中优先级", "低优先级"]  # 严重度降序 + 预算 3
    assert step.state.probes_used == 3
    assert step.violations[0].guard is Guard.PROBE_BUDGET


# --------------------------------------------------------------------------- 护栏: 独白上限


def test_monologue_cap_splits_overflow_into_a_card():
    text = "\n".join(f"第{i}行内容" for i in range(300))
    kept, overflow, violation = enforce_monologue_cap(text, CFG)
    assert violation.guard is Guard.MONOLOGUE_CAP
    assert violation.action is GuardAction.TRUNCATE
    assert len(kept) <= CFG.monologue_cap_chars
    assert overflow  # 溢出部分转附件卡片, 不塞进对话
    assert kept + "\n" + overflow == text  # 内容零丢失


def test_monologue_cap_leaves_short_messages_alone():
    kept, overflow, violation = enforce_monologue_cap("短消息", CFG)
    assert (kept, overflow, violation) == ("短消息", "", None)


def test_supply_budget_is_clamped_to_return_max_chars():
    """Return 单次补给 <= return_max_chars: 严重度高的先拿到预算。"""
    gaps = tuple(
        GapItem(
            type="causal_gap",
            probe=f"probe-{i}",
            severity=3 - i,
            supply=AtomBundle(atoms=(Atom(id=f"atom:{i}", statement="s"),), max_chars=400),
        )
        for i in range(3)
    )
    step = tick(
        in_state(FeynmanState.DETECT, loop_index=1),
        GapReported(report=report(gaps=gaps)),
        config=CFG,
    )
    total = sum(item.supply.max_chars for item in step.state.open_gaps)
    assert total <= CFG.return_max_chars
    assert any("return_max_chars" in note for note in step.notes)


# --------------------------------------------------------------------------- 闭合裁决


def test_closure_verdict_lists_every_unmet_criterion():
    passed, unmet = closure_verdict(
        report(completeness=0.1, own_words=0.1, gaps=(gap(gap_type="jargon_shield"),), wrong=("c1",)),
        transfer_passed=False,
        cfg=CFG,
    )
    assert passed is False
    assert len(unmet) == 6  # 迁移题 / completeness / own_words / 术语盾 / wrong / not_closed


def test_closure_verdict_passes_when_everything_is_met():
    passed, unmet = closure_verdict(report(verdict="closed"), transfer_passed=True, cfg=CFG)
    assert (passed, unmet) == (True, [])


# --------------------------------------------------------------------------- 证据 schema


def test_explanation_turn_derives_chars_and_stays_frozen():
    turn = ExplanationTurn(raw_text="就是一句话")
    assert turn.chars == 5
    assert turn.model_dump()["chars"] == 5  # 派生字段仍进 JSON 契约
    with pytest.raises(ValueError):
        turn.raw_text = "改不了"  # type: ignore[misc]


def test_supported_claim_must_not_carry_a_gap_type():
    with pytest.raises(ValueError, match="gap_type"):
        Claim(id="c1", text="点积", status="supported", gap_type="causal_gap")


def test_atom_kind_decides_the_why_chain_stopping_point():
    assert Atom(id="a", statement="定义", kind="definition").is_terminal
    assert Atom(id="a", statement="公理", kind="axiom").is_terminal
    assert Atom(id="a", statement="可观测量", kind="observable").is_terminal
    assert not Atom(id="a", statement="可推导", kind="derivable").is_terminal


def test_missing_edge_serializes_with_from_to_keys():
    """SSE/JSON 契约用 ``from``/``to``, Python 侧用 ``from_atom``/``to_atom``。"""
    edge = MissingEdge.model_validate({"from": "atom:a", "to": "atom:b"})
    assert edge.model_dump(by_alias=True) == {"from": "atom:a", "to": "atom:b"}


# --------------------------------------------------------------------------- 配置装配


def test_config_from_settings_reads_pedagogy_env(monkeypatch):
    monkeypatch.setenv("PEDAGOGY_MAX_LOOPS", "5")
    monkeypatch.setenv("PEDAGOGY_MIN_EXPLAIN_CHARS", "40")
    monkeypatch.setenv("PEDAGOGY_MIN_LEARNER_TALK_RATIO", "0.7")
    cfg = PedagogyConfig.from_settings(Settings(_env_file=None))
    assert cfg.max_loops == 5
    assert cfg.min_explain_chars == 40
    assert cfg.min_learner_talk_ratio == pytest.approx(0.7)


def test_defaults_match_the_design_doc():
    assert CFG.min_explain_chars == 80
    assert CFG.max_loops == 3
    assert CFG.max_probes_per_turn == 3
    assert CFG.monologue_cap_chars == 800
    assert CFG.return_max_chars == 600
    assert CFG.min_learner_talk_ratio == 0.5
    assert CFG.min_completeness == 0.8
    assert CFG.min_own_words == 0.6
    assert CFG.why_chain_max_depth == 5


# --------------------------------------------------------------------------- 端到端(纯内核)


def test_full_feynman_loop_closes_and_produces_evidence():
    """验收标准: 一句「我想搞懂 X」能走完 Pick→Teach→Detect→Return→Simplify→Closed。"""
    state = PedagogyState(thread_id="t-1", account_id=7)
    trail: list[FeynmanState] = [state.state]

    state = tick(state, ConceptNamed(concept_id="attention-scaled-dot-product"), config=CFG).state
    trail.append(state.state)

    state = tick(state, ExplanationSubmitted(chars=120, system_chars=90), config=CFG).state
    trail.append(state.state)

    state = tick(state, GapReported(report=report(gaps=(gap(),))), config=CFG).state
    trail.append(state.state)

    state = tick(
        state,
        SupplyCompleted(bundle=AtomBundle(atoms=(Atom(id="atom:variance", statement="方差随 d 增长"),))),
        config=CFG,
    ).state
    trail.append(state.state)

    state = tick(state, ExplanationSubmitted(chars=180, system_chars=60), config=CFG).state
    trail.append(state.state)

    state = tick(state, GapReported(report=report(verdict="closed")), config=CFG).state
    trail.append(state.state)

    step = tick(
        state, SimplifyJudged(report=report(verdict="closed"), transfer_passed=True), config=CFG
    )
    trail.append(step.phase)

    assert trail == [
        FeynmanState.PICK,
        FeynmanState.TEACH,
        FeynmanState.DETECT,
        FeynmanState.RETURN,
        FeynmanState.TEACH,
        FeynmanState.DETECT,
        FeynmanState.SIMPLIFY,
        FeynmanState.CLOSED,
    ]
    assert step.terminal
    assert step.actions == (Action.RECORD_MASTERY, Action.SCHEDULE_REVIEW)
    assert step.state.loop_index == 2
    # 学习者说得比系统多 -> 没有触发发言占比护栏
    assert step.state.talk_ratio > CFG.min_learner_talk_ratio
    assert step.state.last_scores is not None
