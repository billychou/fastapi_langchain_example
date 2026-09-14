"""教学内核接入 agent 的中间件(LangChain AgentMiddleware, 不是 Starlette 中间件)。

它做四件事, 每件都对应 ``docs/pedagogy-core.md`` 里的一条设计约束:

1. **注入状态**(``awrap_model_call``): 把本轮的状态快照渲染成一个 ``pedagogy_state``
   块追加到系统提示。状态机在服务端 —— 模型只做「当前状态该做的事」, 不自行宣布转移
   (``skills/feynman-tutor/SKILL.md`` 铁律 1);
2. **解析教练侧事件**(``aafter_model``): 从 ``AIMessage.additional_kwargs["pedagogy_events"]``
   读出 ``gap_reported`` / ``supply_completed`` / ``simplify_judged`` 并 ``tick``;
3. **执行护栏**: ``no_direct_answer`` 与 ``monologue_cap`` 在这里真正改写模型输出
   (``policy.py`` 只提供纯函数, 没有人调用就等于没有护栏);
4. **写回跨轮载体**: 把 :class:`LoopCarrier` 写进 LangGraph 的 ``pedagogy`` 频道,
   因此**不新增任何存储**, 且天然按 ``thread_id`` 隔离。

两个刻意的设计取舍:

- **护栏跑在流式输出之后。** ``aafter_model`` 拿到的是聚合完成的整条消息, 而 token
  增量早已推给浏览器, 服务端收不回来。所以 P0 的策略是「照实改写 + 明确告知」:
  改写后的全文随 ``learn.guard.data.replacement`` 下发, 由前端替换气泡内容(#9),
  checkpointer 里存的也是改写后的版本。假装没发生(悄悄丢弃)会让「学习者看到的」与
  「账本记的」分叉, 那比被截断更糟;
- **``pedagogy_sink is None`` 时零改动。** 闲聊、匿名、``PEDAGOGY_ENABLED=false``
  三种情况都不会走到这里, 普通聊天的行为与注入前逐字一致(可断言, 见
  ``tests/test_chat_api.py``)。
"""

from __future__ import annotations

import logging
from typing import Any, NotRequired

from langchain.agents.middleware import AgentMiddleware, ModelRequest
from langchain.agents.middleware.types import AgentState
from langchain_core.messages import AIMessage, SystemMessage
from pydantic import ValidationError

from app.context import ChatContext
from app.core.messages import message_text
from app.pedagogy.events import LearnEvent, LoopCarrier
from app.pedagogy.loop import LoopStep, tick
from app.pedagogy.persona import recommend_persona
from app.pedagogy.policy import (
    PedagogyConfig,
    check_talk_ratio,
    enforce_monologue_cap,
    enforce_no_direct_answer,
    remaining_probes,
)
from app.pedagogy.schemas import EVENT_TYPES, GapReported, PedagogyState

logger = logging.getLogger("pedagogy.middleware")

#: LangGraph 状态频道名: ``LoopCarrier`` 跨轮存放的地方。
STATE_CHANNEL = "pedagogy"
#: 模型自报教练侧事件的 ``additional_kwargs`` 键。
EVENTS_KEY = "pedagogy_events"
#: 注入系统提示的状态块字数上限。
#:
#: 刻意**不放进** ``PedagogyConfig``/``Settings``: 那是「教学阈值」的配置面
#: (``PEDAGOGY_*``), 且 ``tests/test_pedagogy_skills.py`` 会把它逐项对账到
#: ``loop-sop.md`` 的配置表 —— 一个纯提示词预算混进去, 就得在教练手册里凭空多一行
#: 与教学无关的数字。提示词预算与 ``SKILL_MAX_*_CHARS`` 同类, 属实现常量。
STATE_BLOCK_MAX_CHARS = 1200
#: ``monologue_cap`` 溢出卡片的标题。
ATTACHMENT_TITLE = "被折叠的长内容（monologue_cap 溢出）"

#: 模型可以自报的事件(教练侧)。学习者侧事件由服务端从请求文本判读
#: (``app/pedagogy/turn.py``) —— 让模型自报「学习者交了一份自述」等于让被考核的人
#: 自己填考勤表。
TUTOR_EVENT_KINDS: frozenset[str] = frozenset(
    {"gap_reported", "supply_completed", "simplify_judged"}
)


class PedagogyAgentState(AgentState):
    """扩展 agent 状态: 多一个跨轮持久化的 ``pedagogy`` 频道。

    LangChain 1.x 会把 middleware 的 ``state_schema`` 合并进图状态, 因此这个频道
    **自动**跟着 checkpointer 走(sqlite/postgres 都一样), 不需要新表、新键、新 TTL。
    值是 ``LoopCarrier.to_channel()`` 的 JSON 字典 —— 存字典而不是模型对象, 是因为
    频道要能被 JSON 序列化器写进 checkpoint blob。
    """

    pedagogy: NotRequired[dict[str, Any] | None]


def _scope(context: object) -> ChatContext:
    return context if isinstance(context, ChatContext) else ChatContext()


# --------------------------------------------------------------------------- 状态块渲染


def render_state_block(
    state: PedagogyState,
    step: LoopStep | None,
    config: PedagogyConfig,
    *,
    max_chars: int = STATE_BLOCK_MAX_CHARS,
) -> str:
    """把状态快照渲染成注入系统提示的 ``pedagogy_state`` 块。

    分「必须保留」与「可以丢弃」两段, 超长时**先丢细节再硬截断**:

    - 必须保留: 状态/圈数/概念、内核要求的动作与理由、剩余追问预算、发言占比、
      预算数字、推荐 persona。少了任何一项, 模型就会自己猜一个阈值, 而它猜的
      几乎一定与内核执行的不一样(技能手册里承诺 3 个追问、内核只放 2 个);
    - 可以丢弃: 卡点的 ``probe`` 明细、内核备注、触顶提示、上一轮 rubric 分数。
      这些是「锦上添花」, 但也是最长的部分。

    预算数字与 ``learn.state`` 事件同源(都走 ``remaining_probes``), 所以模型看到的
    和前端显示的一定一致。
    """
    cfg = config
    persona = recommend_persona(state)
    head = [
        "## pedagogy_state（服务端状态机；本轮唯一事实来源）",
        "",
        f"- 状态: {state.state.value}（第 {max(state.loop_index, 0)} 圈 / 上限 {cfg.max_loops}）",
    ]
    if state.concept_id:
        head.append(f"- 概念: {state.concept_id}")
    if step is not None:
        actions = "、".join(action.value for action in step.actions) or "（无）"
        head.append(f"- 内核要求的动作: {actions}")
        if step.reason:
            head.append(f"- 裁决理由: {step.reason}")
    else:
        head.append("- 内核要求的动作: （本轮学习者侧未产生事件，状态不变；按当前状态的 SOP 说话）")
    head.extend(
        [
            f"- 追问预算: 还剩 {remaining_probes(state.probes_used, cfg)} 个"
            f"（超出会被 probe_budget 静默抑制）",
            f"- 发言占比: 学习者 {state.talk_ratio:.2f}"
            f"（下限 {cfg.min_learner_talk_ratio:.2f}，低于则记为教学质量事件）",
            f"- 预算: 自述≥{cfg.min_explain_chars}字·单条≤{cfg.monologue_cap_chars}字"
            f"·Return补给≤{cfg.return_max_chars}字·循环上限{cfg.max_loops}圈",
            f"- 推荐 persona: {persona}（用 load_skill 读它的 SOP 再开口）",
        ]
    )

    details: list[str] = []
    if state.loop_capped:
        details.append(
            "- 循环已触顶: 不要再出同样要求的第 4 轮，改为把概念切小一半或退回 Pick 重设范围"
        )
    for gap in state.open_gaps:
        where = f"（claim {gap.claim_id}）" if gap.claim_id else ""
        details.append(
            f"- 未闭合卡点[{gap.type} severity={gap.severity}]{where}: {gap.probe}"
        )
    for note in (step.notes if step is not None else ()):
        details.append(f"- 内核备注: {note}")
    if state.last_scores is not None:
        scores = state.last_scores
        details.append(
            "- 上一轮 rubric: completeness "
            f"{scores.completeness:.2f}/own_words {scores.own_words:.2f}"
            f"/boundary {scores.boundary_awareness:.2f}"
            f"（闭合门槛 {cfg.min_completeness:.2f}/{cfg.min_own_words:.2f}）"
        )

    text = "\n".join(head)
    if details:
        text = f"{text}\n\n" + "\n".join(details)
    if len(text) <= max_chars:
        return text
    text = "\n".join(head)
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…（状态块超长已截断）"


# --------------------------------------------------------------------------- 事件解析


def parse_tutor_events(raw: Any) -> tuple[tuple[Any, ...], tuple[str, ...]]:
    """把 ``additional_kwargs[EVENTS_KEY]`` 解析成内核事件; 返回 ``(事件, 说明)``。

    三条拒绝规则, 每条都会产出一条 ``learn.note`` 让前端能解释「为什么状态没动」:

    - **学习者侧事件**(``concept_named`` / ``explanation_submitted`` /
      ``escape_requested``)一律拒绝: 它们由服务端从请求文本判读, 模型自报等于自己
      给自己发学分;
    - 未知 ``kind`` 拒绝: 词表是 ``EventKind`` 的六个值, 不自创;
    - 载荷不合法拒绝: 宁可停在原状态, 也不要把半个 GapReport 塞进账本。

    ``system_chars`` 一律强制归零: 系统说了多少字由中间件用 ``len(text)`` 记**一次**,
    模型自报的字数既不可信也会造成重复记账(``learner_talk_ratio`` 护栏因此失真)。
    """
    if raw is None:
        return (), ()
    if not isinstance(raw, (list, tuple)):
        return (), (f"{EVENTS_KEY} 必须是列表, 收到 {type(raw).__name__}; 本轮忽略",)

    events: list[Any] = []
    notes: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            notes.append(f"{EVENTS_KEY} 里有一个非对象元素({type(item).__name__}), 已忽略")
            continue
        kind = item.get("kind")
        if not isinstance(kind, str) or kind not in EVENT_TYPES:
            notes.append(f"未知事件类型 {kind!r}(词表见 EventKind), 已忽略")
            continue
        if kind not in TUTOR_EVENT_KINDS:
            notes.append(f"{kind} 属学习者侧事件, 由服务端判读, 不接受模型自报; 已忽略")
            continue
        payload = {**item, "system_chars": 0}
        try:
            events.append(EVENT_TYPES[kind].model_validate(payload))
        except ValidationError as exc:
            logger.warning("pedagogy 事件 %s 载荷非法: %s", kind, exc)
            notes.append(f"{kind} 载荷无法解析, 已忽略(状态保持原样)")
    return tuple(events), tuple(notes)


# --------------------------------------------------------------------------- 中间件


class PedagogyMiddleware(AgentMiddleware[PedagogyAgentState, ChatContext]):
    """把服务端状态机接到单例 agent 上。

    ``config`` 显式传入时用传入值(测试固定阈值), 否则每次从 ``Settings`` 装配 ——
    ``get_settings()`` 是 lru_cache 的, 所以这不会带来每轮 IO。
    """

    state_schema = PedagogyAgentState

    def __init__(self, config: PedagogyConfig | None = None) -> None:
        self._config = config

    @property
    def config(self) -> PedagogyConfig:
        return self._config or PedagogyConfig.from_settings()

    # ------------------------------------------------------------------ 注入

    async def awrap_model_call(self, request: ModelRequest, handler):
        scope = _scope(request.runtime.context)
        sink, state = scope.pedagogy_sink, scope.pedagogy_state
        if sink is None or state is None:
            return await handler(request)

        block = render_state_block(
            state, scope.pedagogy_step, self.config, max_chars=STATE_BLOCK_MAX_CHARS
        )
        system_message = request.system_message
        base = system_message.content if isinstance(system_message, SystemMessage) else ""
        base = base if isinstance(base, str) else ""
        merged = SystemMessage(content=f"{base}\n\n{block}" if base else block)
        # 工具循环里模型会被调用多次, 每次的 system_message 都从原始提示重建,
        # 所以这里每次都追加是正确的(不会累积), 也不需要 sink.mark 去重。
        return await handler(request.override(system_message=merged))

    # ------------------------------------------------------------------ 裁决与护栏

    async def aafter_model(
        self, state: PedagogyAgentState, runtime: Any
    ) -> dict[str, Any] | None:
        scope = _scope(runtime.context)
        sink = scope.pedagogy_sink
        if sink is None:
            # 不是教学回合: 一个字节都不改(频道原样保留, 普通聊天行为逐字一致)。
            return None

        carrier = sink.carrier or LoopCarrier.from_channel(state.get(STATE_CHANNEL))
        if carrier is None and scope.pedagogy_state is not None:
            carrier = LoopCarrier(state=scope.pedagogy_state)
        if carrier is None:
            return None

        messages = state.get("messages") or ()
        last = messages[-1] if messages else None
        if not isinstance(last, AIMessage):
            return {STATE_CHANNEL: carrier.to_channel()}
        if sink.carrier is not None and not sink.mark(f"accounted:{last.id}"):
            # 同一条消息被 after_model 处理过(图重入/重试): 再算一次会重复记账,
            # 也会重复下发 learn 事件。
            return {STATE_CHANNEL: carrier.to_channel()}

        cfg = self.config
        text = message_text(last.content)
        current = carrier.state

        events, notes = parse_tutor_events((last.additional_kwargs or {}).get(EVENTS_KEY))
        for note in notes:
            sink.emit(LearnEvent.of_note(note, current))
        for event in events:
            step = tick(current, event, config=cfg)
            carrier = _advance(carrier, step.state)
            current = step.state
            sink.emit(
                LearnEvent.of_state(
                    current,
                    actions=tuple(action.value for action in step.actions),
                    reason=step.reason,
                    persona=recommend_persona(current),
                    probes_left=remaining_probes(current.probes_used, cfg),
                    loop_capped=current.loop_capped,
                    notes=step.notes,
                )
            )
            if isinstance(event, GapReported):
                # 只有诊断结论才带来新卡点; 补给/简化裁决的 open_gaps 是旧账, 不重发。
                for gap in current.open_gaps:
                    sink.emit(LearnEvent.of_gap(gap, current))
            for violation in step.violations:
                sink.emit(LearnEvent.of_guard(violation, current))

        # 护栏 1: 禁止直答(Detect/Return)。必须在字数上限之前跑 —— 反过来会让
        # 「先折叠溢出、再截断解法」把改写提示切进卡片里。
        guarded, violation = enforce_no_direct_answer(current.state, text, cfg)
        if violation is not None:
            sink.emit(LearnEvent.of_guard(violation, current, replacement=guarded))
        # 护栏 2: 单条消息字数上限, 溢出转附件卡片。
        kept, overflow, violation = enforce_monologue_cap(guarded, cfg)
        if violation is not None:
            sink.emit(LearnEvent.of_guard(violation, current, replacement=kept))
            sink.emit(
                LearnEvent.of_attachment(current, title=ATTACHMENT_TITLE, text=overflow)
            )

        # 记账: 系统这一条说了多少字(learner_talk_ratio 的分母)。
        carrier = _advance(
            carrier,
            current.model_copy(update={"system_chars": current.system_chars + len(kept)}),
        )
        current = carrier.state
        if sink.mark("guard:learner_talk_ratio"):
            # 软护栏: 一次请求只记一次(工具循环里模型会被调用多次)。
            ratio_violation = check_talk_ratio(
                current.learner_chars, current.system_chars, cfg
            )
            if ratio_violation is not None:
                sink.emit(LearnEvent.of_guard(ratio_violation, current))

        sink.carrier = carrier
        updates: dict[str, Any] = {STATE_CHANNEL: carrier.to_channel()}
        if kept != text:
            # 按 id 替换(add_messages 的语义): checkpointer 里存的是改写后的版本。
            updates["messages"] = [last.model_copy(update={"content": kept})]
        return updates


def _advance(carrier: LoopCarrier, state: PedagogyState) -> LoopCarrier:
    """把新快照装回载体(``LoopCarrier`` 冻结, 只能重建)。"""
    return carrier.model_copy(update={"state": state})


__all__ = [
    "ATTACHMENT_TITLE",
    "EVENTS_KEY",
    "STATE_BLOCK_MAX_CHARS",
    "STATE_CHANNEL",
    "TUTOR_EVENT_KINDS",
    "PedagogyAgentState",
    "PedagogyMiddleware",
    "parse_tutor_events",
    "render_state_block",
]
