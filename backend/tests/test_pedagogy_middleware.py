"""``PedagogyMiddleware``: 状态注入、事件解析、护栏改写、跨轮频道写回。

四件事对应 ``docs/pedagogy-core.md`` 的四条设计约束, 也就分成四组断言:

1. **注入**: 状态块进系统提示, 且预算数字与 ``learn.state`` 事件同源(同一个
   ``remaining_probes``) —— 模型以为还能问 3 个、前端显示还剩 1 个, 学习者看到的就是
   自相矛盾的界面;
2. **解析**: 只认教练侧三类事件; 学习者侧事件、未知 ``kind``、坏载荷一律拒绝并给出
   ``learn.note``(让前端能解释「为什么状态没动」), ``system_chars`` 强制归零;
3. **护栏**: ``no_direct_answer`` 与 ``monologue_cap`` 在这里**真的改写**模型输出,
   改写结果既写回消息历史(checkpointer 存的就是它), 也随 ``learn.guard`` 下发;
4. **零打扰**: ``pedagogy_sink is None`` 时一个字节都不改(闲聊/匿名/降级开关)。

图级用例走真实路径: ``create_agent`` + sqlite checkpointer + 流式假模型(护栏跑在
token 已经出门之后, 这是 P0 的刻意取舍, 见 ``agent_pedagogy`` 模块文档)。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import aiosqlite
import pytest
from app.context import ChatContext
from app.middleware.agent_pedagogy import (
    ATTACHMENT_TITLE,
    EVENTS_KEY,
    STATE_BLOCK_MAX_CHARS,
    STATE_CHANNEL,
    TUTOR_EVENT_KINDS,
    PedagogyAgentState,
    PedagogyMiddleware,
    parse_tutor_events,
    render_state_block,
)
from app.pedagogy.events import LearnEvent, LoopCarrier, PedagogySink
from app.pedagogy.loop import tick
from app.pedagogy.policy import (
    DIRECT_ANSWER_REWRITE_HINT,
    PedagogyConfig,
    enforce_monologue_cap,
    enforce_no_direct_answer,
    remaining_probes,
)
from app.pedagogy.schemas import (
    Atom,
    AtomBundle,
    GapItem,
    GapReported,
    GapScores,
)
from app.pedagogy.states import FeynmanState
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from pedagogy_helpers import (
    ACCOUNT_ID,
    THREAD_ID,
    ScriptedTutor,
    gap_event,
    make_context,
    make_state,
    simplify_event,
    supply_event,
    with_events,
)

BASE_PROMPT = "BASE_SYSTEM_PROMPT"
CFG = PedagogyConfig()
DIRECT_ANSWER = "1. 先求导\n2. 再积分\n3. 最后代入\n答案：42"
BENIGN_REPLY = "你刚才那一步为什么可以这么做？先说说它依赖了什么。"


def gap_item(gap_type: str = "jargon_shield", severity: int = 3, probe: str = "不许用这个词，重说一遍") -> GapItem:
    return GapItem(
        type=gap_item_type(gap_type),
        claim_id="c1",
        probe=probe,
        severity=severity,
        supply=AtomBundle(atoms=(Atom(id="atom:a", statement="闭包捕获变量本身"),), max_chars=300),
    )


def gap_item_type(gap_type: str):
    return gap_type  # type: ignore[return-value]


@asynccontextmanager
async def tutor_graph(
    model: ScriptedTutor, config: PedagogyConfig = CFG, tmp_path=None
) -> AsyncIterator[Any]:
    """与 ``app/agent.py`` 同样的接线方式(单 middleware + context_schema + checkpointer)。"""
    conn = await aiosqlite.connect(f"{tmp_path}/ckpt.sqlite" if tmp_path else ":memory:")
    saver = AsyncSqliteSaver(conn)
    await saver.setup()
    try:
        yield create_agent(
            model=model,
            tools=[],
            system_prompt=BASE_PROMPT,
            middleware=[PedagogyMiddleware(config)],
            context_schema=ChatContext,
            checkpointer=saver,
        )
    finally:
        await conn.close()


async def run_turn(
    graph,
    context: ChatContext,
    text: str = "我讲一遍",
    thread_id: str = THREAD_ID,
    config: dict[str, Any] | None = None,
):
    config = config or {"configurable": {"thread_id": thread_id}}
    result = await graph.ainvoke({"messages": [HumanMessage(content=text)]}, context=context, config=config)
    snapshot = await graph.aget_state(config)
    return result, (snapshot.values or {}).get(STATE_CHANNEL), config


def events_of(sink: PedagogySink | None, *types: str) -> list[LearnEvent]:
    assert sink is not None
    return [event for event in sink.drain() if event.type in types]


def guards(sink: PedagogySink | None) -> list[dict]:
    return [event.to_sse()["data"] for event in events_of(sink, "guard")]


# --------------------------------------------------------------------------- 状态块渲染


def test_state_block_carries_every_head_field():
    state = make_state(FeynmanState.DETECT, loop_index=2)
    step = tick(state, GapReported(report=__import__("app.pedagogy.schemas", fromlist=["GapReport"]).GapReport(), system_chars=0), config=CFG)
    block = render_state_block(step.state, step, CFG)
    assert block.startswith("## pedagogy_state")
    assert BASE_PROMPT not in block  # 只渲染状态块本身, 拼接由中间件负责
    assert "状态: Simplify" in block or "状态: Detect" in block
    assert f"概念: {state.concept_id}" in block
    assert "内核要求的动作:" in block
    assert f"还剩 {remaining_probes(step.state.probes_used, CFG)} 个" in block
    assert f"下限 {CFG.min_learner_talk_ratio:.2f}" in block
    assert f"自述≥{CFG.min_explain_chars}字" in block
    assert f"单条≤{CFG.monologue_cap_chars}字" in block
    assert f"循环上限{CFG.max_loops}圈" in block
    assert "推荐 persona:" in block


def test_state_block_without_step_tells_the_model_to_follow_the_sop():
    block = render_state_block(make_state(FeynmanState.TEACH), None, CFG)
    assert "本轮学习者侧未产生事件" in block
    assert "状态: Teach" in block


def test_state_block_includes_details_when_within_budget():
    state = make_state(FeynmanState.RETURN).model_copy(
        update={
            "open_gaps": (gap_item(),),
            "loop_capped": True,
            "last_scores": GapScores(completeness=0.4, own_words=0.3, boundary_awareness=0.1),
        }
    )
    block = render_state_block(state, None, CFG)
    assert "循环已触顶" in block
    assert "未闭合卡点[jargon_shield severity=3]" in block
    assert "不许用这个词" in block
    assert "上一轮 rubric" in block


def test_state_block_drops_details_before_truncating_the_head():
    """超长时**先丢细节再硬截断**: 预算数字属于必须保留的部分。"""
    state = make_state(FeynmanState.RETURN).model_copy(
        update={"open_gaps": tuple(gap_item(probe=f"很长的追问{i}" * 20) for i in range(6))}
    )
    generous = render_state_block(state, None, CFG, max_chars=2000)
    assert "很长的追问0" in generous

    # 默认预算就放不下这 6 条卡点明细: 头部完整保留、细节整段丢弃, 不出现截断标记。
    full = render_state_block(state, None, CFG)
    assert len(full) <= STATE_BLOCK_MAX_CHARS
    assert "很长的追问0" not in full
    assert "追问预算:" in full
    assert "…" not in full

    tight = render_state_block(state, None, CFG, max_chars=420)
    assert len(tight) <= 420
    assert "很长的追问0" not in tight  # 细节被丢掉
    assert "追问预算:" in tight  # 头部保留
    assert "…" not in tight  # 没到硬截断


def test_state_block_hard_truncates_when_even_the_head_is_too_long():
    state = make_state(FeynmanState.DETECT)
    block = render_state_block(state, None, CFG, max_chars=40)
    assert block.endswith("…（状态块超长已截断）")
    assert len(block) <= 40 + len("…（状态块超长已截断）")


def test_state_block_default_budget_is_the_module_constant():
    """预算常量刻意不进 ``PedagogyConfig``: 它是提示词预算, 不是教学阈值。

    混进去就得在 ``loop-sop.md`` 的配置表里凭空多一行与教学无关的数字, 而
    ``test_pedagogy_skills.py`` 会逐项对账那张表。
    """
    assert STATE_BLOCK_MAX_CHARS == 1200
    assert "STATE_BLOCK_MAX_CHARS" not in PedagogyConfig.model_fields
    assert "state_block_max_chars" not in PedagogyConfig.model_fields


def test_probe_budget_in_block_matches_the_state_event():
    state = make_state(FeynmanState.RETURN).model_copy(update={"probes_used": 2})
    block = render_state_block(state, None, CFG)
    event = LearnEvent.of_state(state, probes_left=remaining_probes(state.probes_used, CFG))
    assert f"还剩 {event.to_sse()['data']['probes_left']} 个" in block


# --------------------------------------------------------------------------- 事件解析


def test_parse_accepts_the_three_tutor_events():
    events, notes = parse_tutor_events([gap_event(), supply_event(), simplify_event()])
    assert notes == ()
    assert [event.kind for event in events] == [
        "gap_reported",
        "supply_completed",
        "simplify_judged",
    ]
    assert set(TUTOR_EVENT_KINDS) == {"gap_reported", "supply_completed", "simplify_judged"}


def test_parse_none_is_empty():
    assert parse_tutor_events(None) == ((), ())


def test_parse_rejects_learner_side_events():
    """让模型自报「学习者交了一份自述」等于让被考核的人自己填考勤表。"""
    events, notes = parse_tutor_events([{"kind": "explanation_submitted", "chars": 500}])
    assert events == ()
    assert "不接受模型自报" in notes[0]


@pytest.mark.parametrize("kind", ["concept_named", "explanation_submitted", "escape_requested"])
def test_parse_rejects_every_learner_side_kind(kind: str):
    events, notes = parse_tutor_events([{"kind": kind}])
    assert events == ()
    assert notes


@pytest.mark.parametrize("kind", ["", "gap_closed", "mastery_updated", None, 42])
def test_parse_rejects_unknown_kinds(kind):
    events, notes = parse_tutor_events([{"kind": kind}])
    assert events == ()
    assert "未知事件类型" in notes[0]


def test_parse_rejects_non_list_payload():
    events, notes = parse_tutor_events({"kind": "gap_reported"})
    assert events == ()
    assert "必须是列表" in notes[0]


def test_parse_skips_non_dict_items():
    events, notes = parse_tutor_events(["gap_reported", 7, gap_event()])
    assert len(events) == 1
    assert sum("非对象元素" in note for note in notes) == 2


def test_parse_rejects_invalid_payload_and_keeps_state():
    """宁可停在原状态, 也不要把半个 GapReport 塞进账本。"""
    events, notes = parse_tutor_events([{"kind": "gap_reported", "report": {"gaps": "不是列表"}}])
    assert events == ()
    assert "载荷无法解析" in notes[0]


def test_parse_forces_system_chars_to_zero():
    """系统说了多少字由中间件按 ``len(text)`` 记一次; 模型自报的字数不可信。"""
    events, _ = parse_tutor_events([gap_event(system_chars=999), supply_event(system_chars=4242)])
    assert [event.system_chars for event in events] == [0, 0]


def test_parse_accepts_tuples_and_mixes_valid_with_invalid():
    events, notes = parse_tutor_events((gap_event(), {"kind": "nope"}))
    assert len(events) == 1
    assert len(notes) == 1


# --------------------------------------------------------------------------- 零打扰


async def test_non_teaching_turn_changes_nothing(tmp_path):
    """``pedagogy_sink is None``: 普通聊天的行为与注入前逐字一致。"""
    model = ScriptedTutor(script=[(BENIGN_REPLY, {})])
    async with tutor_graph(model, tmp_path=tmp_path) as graph:
        context, _ = make_context(None)
        result, channel, _ = await run_turn(graph, context)

    assert model.seen_prompts and all(BASE_PROMPT in prompt for prompt in model.seen_prompts)
    assert all("pedagogy_state" not in prompt for prompt in model.seen_prompts)
    assert result["messages"][-1].content == BENIGN_REPLY
    assert channel is None


async def test_after_model_returns_none_without_a_sink():
    middleware = PedagogyMiddleware(CFG)
    state: PedagogyAgentState = {"messages": [AIMessage(content=DIRECT_ANSWER, id="m1")]}
    runtime = SimpleNamespace(context=ChatContext())
    assert await middleware.aafter_model(state, runtime) is None


async def test_after_model_writes_channel_when_message_is_not_ai():
    """载体来自频道而本轮最后一条不是 AI 消息: 仍要把频道原样写回, 但不改写任何文本。"""
    middleware = PedagogyMiddleware(CFG)
    carrier = LoopCarrier(state=make_state(FeynmanState.TEACH), turns=1)
    context, sink = make_context(make_state(FeynmanState.TEACH), carrier=carrier)
    state = {"messages": [HumanMessage(content="我讲一遍")], STATE_CHANNEL: carrier.to_channel()}
    updates = await middleware.aafter_model(state, SimpleNamespace(context=context))
    assert updates == {STATE_CHANNEL: carrier.to_channel()}
    assert sink is not None and sink.emitted == 0


# --------------------------------------------------------------------------- 注入(图级)


async def test_state_block_is_injected_into_the_system_prompt(tmp_path):
    model = ScriptedTutor(script=[(BENIGN_REPLY, {})])
    async with tutor_graph(model, tmp_path=tmp_path) as graph:
        context, _ = make_context(make_state(FeynmanState.DETECT, loop_index=2))
        await run_turn(graph, context)

    assert model.seen_prompts, "模型未被调用"
    prompt = model.seen_prompts[0]
    assert prompt.startswith(BASE_PROMPT)
    assert "## pedagogy_state" in prompt
    assert "状态: Detect" in prompt
    assert "第 2 圈" in prompt
    assert "推荐 persona: feynman-tutor" in prompt


async def test_injected_block_reflects_the_step_from_the_route(tmp_path):
    """路由装配的学习者侧转移必须出现在这一轮的提示里(模型当轮就按新状态说话)。"""
    state = make_state(FeynmanState.TEACH)
    step = tick(
        state,
        __import__("app.pedagogy.schemas", fromlist=["ExplanationSubmitted"]).ExplanationSubmitted(
            chars=120, text="x" * 120, system_chars=0
        ),
        config=CFG,
    )
    model = ScriptedTutor(script=[(BENIGN_REPLY, {})])
    async with tutor_graph(model, tmp_path=tmp_path) as graph:
        context, _ = make_context(step.state, step=step)
        await run_turn(graph, context)

    prompt = model.seen_prompts[0]
    assert "状态: Detect" in prompt
    assert "内核要求的动作: probe" in prompt


# --------------------------------------------------------------------------- 频道写回


async def test_channel_records_the_carrier_after_a_plain_reply(tmp_path):
    model = ScriptedTutor(script=[(BENIGN_REPLY, {})])
    async with tutor_graph(model, tmp_path=tmp_path) as graph:
        context, _ = make_context(make_state(FeynmanState.TEACH))
        _, channel, _ = await run_turn(graph, context)

    assert channel is not None
    assert channel["state"]["state"] == "Teach"
    assert channel["state"]["concept_id"] == "闭包"
    # 记账口径: 系统这一条说了多少字 = len(改写后的文本)
    assert channel["state"]["system_chars"] == len(BENIGN_REPLY)
    assert channel["turns"] == 1


async def test_channel_survives_across_turns(tmp_path):
    """跨轮持久化靠 checkpointer 的 ``pedagogy`` 频道, 不新增任何存储。"""
    model = ScriptedTutor(script=[("第一轮的追问。", {}), ("第二轮的追问。", {})])
    async with tutor_graph(model, tmp_path=tmp_path) as graph:
        first_ctx, _ = make_context(make_state(FeynmanState.TEACH))
        _, first_channel, config = await run_turn(graph, first_ctx)
        assert first_channel is not None

        # 第二轮: 路由会从频道读回载体(与 app/api/chat.py 的 _prepare_pedagogy 同构)
        resumed = LoopCarrier.from_channel(first_channel)
        assert resumed is not None
        sink = PedagogySink()
        sink.carrier = resumed.model_copy(update={"turns": resumed.turns + 1})
        second_ctx = ChatContext(
            account_id=ACCOUNT_ID,
            authenticated=True,
            permissions=frozenset({"*"}),
            learner_id=ACCOUNT_ID,
            pedagogy_state=resumed.state,
            pedagogy_sink=sink,
        )
        result, second_channel, _ = await run_turn(graph, second_ctx, config=config)

    assert result["messages"][-1].content == "第二轮的追问。"
    assert second_channel["state"]["system_chars"] == len("第一轮的追问。") + len("第二轮的追问。")
    assert second_channel["turns"] == 2


async def test_unknown_thread_has_no_channel_and_does_not_raise(tmp_path):
    model = ScriptedTutor(script=[(BENIGN_REPLY, {})])
    async with tutor_graph(model, tmp_path=tmp_path) as graph:
        config = {"configurable": {"thread_id": "thread-never-seen"}}
        snapshot = await graph.aget_state(config)
    assert (snapshot.values or {}).get(STATE_CHANNEL) is None


# --------------------------------------------------------------------------- 教练侧事件(图级)


async def test_gap_report_advances_to_return_and_publishes_evidence(tmp_path):
    model = ScriptedTutor(script=[(BENIGN_REPLY, with_events(gap_event(gap_type="jargon_shield")))])
    async with tutor_graph(model, tmp_path=tmp_path) as graph:
        context, sink = make_context(make_state(FeynmanState.DETECT))
        result, channel, _ = await run_turn(graph, context)

    payloads = [event.to_sse() for event in events_of(sink, "state", "gap")]
    assert payloads[0]["type"] == "state"
    assert payloads[0]["state"] == "Return"
    assert payloads[0]["data"]["actions"] == ["supply_atoms"]
    assert payloads[0]["data"]["persona"] == "feynman-listener"
    assert payloads[1]["type"] == "gap"
    assert payloads[1]["data"]["gap_type"] == "jargon_shield"
    assert payloads[1]["data"]["probe"]

    assert channel is not None
    assert channel["state"]["state"] == "Return"
    assert [item["type"] for item in channel["state"]["open_gaps"]] == ["jargon_shield"]
    assert channel["state"]["probes_used"] == 1
    assert channel["state"]["last_scores"]["completeness"] == 0.5
    # 模型自报的 system_chars=999 被强制归零, 记账只认中间件数出来的字数
    assert channel["state"]["system_chars"] == len(result["messages"][-1].content)


async def test_supply_completed_returns_to_teach(tmp_path):
    model = ScriptedTutor(script=[(BENIGN_REPLY, with_events(supply_event()))])
    state = make_state(FeynmanState.RETURN, loop_index=1)
    async with tutor_graph(model, tmp_path=tmp_path) as graph:
        context, sink = make_context(state)
        _, channel, _ = await run_turn(graph, context)

    assert channel["state"]["state"] == "Teach"
    assert channel["state"]["loop_index"] == 2
    assert channel["state"]["open_gaps"] == []
    assert events_of(sink, "state")[0].to_sse()["data"]["actions"] == ["ask_explain"]


async def test_simplify_judged_closes_the_loop(tmp_path):
    model = ScriptedTutor(script=[(BENIGN_REPLY, with_events(simplify_event()))])
    async with tutor_graph(model, tmp_path=tmp_path) as graph:
        context, sink = make_context(make_state(FeynmanState.SIMPLIFY))
        _, channel, _ = await run_turn(graph, context)

    assert channel["state"]["state"] == "Closed"
    state_event = events_of(sink, "state")[0].to_sse()
    assert state_event["data"]["actions"] == ["record_mastery", "schedule_review"]
    assert state_event["data"]["scores"]["completeness"] == 0.9


async def test_learner_side_event_from_the_model_is_rejected_with_a_note(tmp_path):
    model = ScriptedTutor(
        script=[(BENIGN_REPLY, {EVENTS_KEY: [{"kind": "explanation_submitted", "chars": 900}]})]
    )
    async with tutor_graph(model, tmp_path=tmp_path) as graph:
        context, sink = make_context(make_state(FeynmanState.TEACH))
        _, channel, _ = await run_turn(graph, context)

    notes = [event.to_sse()["data"]["text"] for event in events_of(sink, "note")]
    assert any("不接受模型自报" in note for note in notes)
    assert channel["state"]["state"] == "Teach"  # 状态没动
    assert channel["state"]["learner_chars"] == 0  # 也没记字数


async def test_invalid_event_keeps_state_and_explains(tmp_path):
    model = ScriptedTutor(script=[(BENIGN_REPLY, {EVENTS_KEY: [{"kind": "gap_reported"}]})])
    async with tutor_graph(model, tmp_path=tmp_path) as graph:
        context, sink = make_context(make_state(FeynmanState.DETECT))
        _, channel, _ = await run_turn(graph, context)

    notes = [event.to_sse()["data"]["text"] for event in events_of(sink, "note")]
    assert any("载荷无法解析" in note for note in notes)
    assert channel["state"]["state"] == "Detect"


async def test_reprocessing_the_same_message_does_not_double_account(tmp_path):
    """图重入/重试时同一条消息可能被 ``aafter_model`` 看到两次: 记账与事件都只算一次。"""
    middleware = PedagogyMiddleware(CFG)
    state_snapshot = make_state(FeynmanState.DETECT)
    context, sink = make_context(state_snapshot)
    message = AIMessage(content=BENIGN_REPLY, id="msg-1")
    state = {"messages": [HumanMessage(content="我讲一遍"), message]}
    runtime = SimpleNamespace(context=context)

    first = await middleware.aafter_model(state, runtime)
    second = await middleware.aafter_model(state, runtime)

    assert first is not None and second is not None
    assert first[STATE_CHANNEL]["state"]["system_chars"] == len(BENIGN_REPLY)
    assert second[STATE_CHANNEL]["state"]["system_chars"] == len(BENIGN_REPLY)
    assert sink is not None
    assert len(guards(sink)) == 1  # learner_talk_ratio 只记一次


# --------------------------------------------------------------------------- 护栏(图级)


async def test_direct_answer_is_truncated_and_the_replacement_is_published(tmp_path):
    model = ScriptedTutor(script=[(DIRECT_ANSWER, {})])
    async with tutor_graph(model, tmp_path=tmp_path) as graph:
        context, sink = make_context(make_state(FeynmanState.DETECT))
        result, channel, config = await run_turn(graph, context)
        # checkpointer 里存的也是改写后的版本(「学习者看到的」与「账本记的」不分叉)
        snapshot = await graph.aget_state(config)

    kept = DIRECT_ANSWER.split("1. 先求导")[0].rstrip()
    expected = f"{kept}\n\n{DIRECT_ANSWER_REWRITE_HINT}" if kept else DIRECT_ANSWER_REWRITE_HINT
    assert snapshot.values["messages"][-1].content == expected
    assert result["messages"][-1].content == expected
    assert channel["state"]["system_chars"] == len(expected)

    data = guards(sink)
    assert any(item["guard"] == "no_direct_answer" for item in data)
    replacement = next(item for item in data if item["guard"] == "no_direct_answer")
    assert replacement["replacement"] == expected
    assert replacement["action"] == "truncate"


async def test_rewrite_does_not_duplicate_the_message(tmp_path):
    """按 id 替换(``add_messages`` 语义): 前端不该看到两条 AI 消息。"""
    model = ScriptedTutor(script=[(DIRECT_ANSWER, {})])
    async with tutor_graph(model, tmp_path=tmp_path) as graph:
        context, _ = make_context(make_state(FeynmanState.RETURN))
        result, _, config = await run_turn(graph, context)
        snapshot = await graph.aget_state(config)

    assert len([m for m in result["messages"] if m.type == "ai"]) == 1
    assert len([m for m in snapshot.values["messages"] if m.type == "ai"]) == 1


async def test_monologue_cap_moves_overflow_into_an_attachment(tmp_path):
    long_reply = "先说一句结论。\n" + "补充说明。" * 200
    model = ScriptedTutor(script=[(long_reply, {})])
    config = PedagogyConfig(monologue_cap_chars=60)
    async with tutor_graph(model, config, tmp_path) as graph:
        context, sink = make_context(make_state(FeynmanState.TEACH))
        result, channel, _ = await run_turn(graph, context)

    kept, overflow, violation = enforce_monologue_cap(long_reply, config)
    assert violation is not None
    assert result["messages"][-1].content == kept
    assert len(kept) <= config.monologue_cap_chars

    # ``PedagogySink.drain()`` 是游标式的(读过即走), 所以护栏与卡片必须一次取走再分组。
    emitted = [event.to_sse() for event in sink.drain()]
    cards = [item["data"] for item in emitted if item["type"] == "attachment"]
    assert len(cards) == 1
    assert cards[0]["title"] == ATTACHMENT_TITLE
    assert cards[0]["text"] == overflow
    assert cards[0]["chars"] == len(overflow)
    guard_items = [item["data"] for item in emitted if item["type"] == "guard"]
    assert any(item["guard"] == "monologue_cap" for item in guard_items)
    assert channel["state"]["system_chars"] == len(kept)


async def test_guards_apply_in_documented_order(tmp_path):
    """``no_direct_answer`` 先跑, ``monologue_cap`` 后跑: 顺序反过来会把改写提示切进卡片。"""
    reply = "解题思路如下：\n" + DIRECT_ANSWER + "\n" + "填充内容。" * 200
    model = ScriptedTutor(script=[(reply, {})])
    config = PedagogyConfig(monologue_cap_chars=120)
    async with tutor_graph(model, config, tmp_path) as graph:
        context, _ = make_context(make_state(FeynmanState.DETECT))
        result, _, _ = await run_turn(graph, context)

    guarded, _ = enforce_no_direct_answer(FeynmanState.DETECT, reply, config)
    expected, _, _ = enforce_monologue_cap(guarded, config)
    assert result["messages"][-1].content == expected


async def test_talk_ratio_guard_is_logged_once_per_request(tmp_path):
    """软护栏: 只记录不阻断, 且一次请求只记一次(工具循环里模型会被调用多次)。"""
    model = ScriptedTutor(script=[(BENIGN_REPLY, {})])
    async with tutor_graph(model, tmp_path=tmp_path) as graph:
        context, sink = make_context(make_state(FeynmanState.TEACH, learner_chars=0))
        _, channel, _ = await run_turn(graph, context)

    ratio_guards = [item for item in guards(sink) if item["guard"] == "learner_talk_ratio"]
    assert len(ratio_guards) == 1
    assert ratio_guards[0]["action"] == "log"
    assert "replacement" not in ratio_guards[0]
    assert channel["state"]["learner_chars"] == 0


async def test_talk_ratio_guard_stays_quiet_when_the_learner_talks(tmp_path):
    model = ScriptedTutor(script=[("嗯。", {})])
    async with tutor_graph(model, tmp_path=tmp_path) as graph:
        context, sink = make_context(make_state(FeynmanState.TEACH, learner_chars=500))
        await run_turn(graph, context)

    assert guards(sink) == []


async def test_no_guard_fires_outside_detect_and_return(tmp_path):
    """``Teach`` 下允许讲解: ``no_direct_answer`` 只拦诊断/补给两个状态。"""
    model = ScriptedTutor(script=[(DIRECT_ANSWER, {})])
    async with tutor_graph(model, tmp_path=tmp_path) as graph:
        context, sink = make_context(make_state(FeynmanState.TEACH, learner_chars=900))
        result, _, _ = await run_turn(graph, context)

    assert result["messages"][-1].content == DIRECT_ANSWER
    assert guards(sink) == []
