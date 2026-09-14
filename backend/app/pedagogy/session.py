"""把一轮 HTTP 请求装配成一个**教学回合**(路由侧装配, 纯函数)。

这里是「学习者侧事件」的落点(见 :mod:`app.pedagogy.turn` 的模块文档): 路由在调模型
**之前**就要知道状态机走到哪了, 因为

1. 中间件要把新状态注入系统提示(模型这一轮就得按 ``Teach`` 而不是 ``Pick`` 说话);
2. ``learn`` SSE 事件要能与 token 增量交错下发, 而不是等流结束才补一张状态卡。

装配结果经 ``ChatContext`` 传给单例 agent(``get_agent()`` 是进程级单例, 任何按请求的
差异都只能走 context —— 与 skills 的「身份经 context 传入」同构)。

刻意**不做**的事: 不 ``await`` 任何东西、不查库、不碰 agent 对象。跨轮载体的读取
(``agent.aget_state``)与写回(LangGraph 频道)分别由 ``app/api/chat.py`` 和
``app/middleware/agent_pedagogy.py`` 负责, 本模块因此可以被穷举单测。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.pedagogy.events import LearnEvent, LoopCarrier, PedagogySink
from app.pedagogy.loop import LoopStep, tick
from app.pedagogy.persona import recommend_persona
from app.pedagogy.policy import GuardAction, PedagogyConfig, remaining_probes
from app.pedagogy.schemas import PedagogyState
from app.pedagogy.turn import TurnReading, read_turn, wants_to_learn


@dataclass(frozen=True, slots=True)
class PedagogyTurn:
    """一轮教学请求的装配结果。

    - ``carrier``   本轮结束时应写回 checkpointer 的载体(中间件会继续推进它);
    - ``step``      学习者侧事件触发的转移; ``None`` 表示这一轮状态没动(例如在 ``Detect``
      里说了句闲话 —— 推进需要判分器, 属教练侧事件);
    - ``sink``      本次请求的 ``learn`` 事件收集器(已装入 ``carrier``, 中间件据此续写);
    - ``pre_events`` 调模型**之前**就能确定的事件(状态卡 / 护栏 / 说明), 路由直接下发。
    """

    carrier: LoopCarrier
    step: LoopStep | None
    sink: PedagogySink
    pre_events: tuple[LearnEvent, ...] = ()

    @property
    def state(self) -> PedagogyState:
        return self.carrier.state

    @property
    def context_kwargs(self) -> dict[str, Any]:
        """``ChatContext(**身份字段, **turn.context_kwargs)`` 直接展开的四个教学字段。"""
        return {
            "learner_id": self.carrier.state.learner_id,
            "pedagogy_state": self.carrier.state,
            "pedagogy_step": self.step,
            "pedagogy_sink": self.sink,
        }


def prepare_turn(
    *,
    raw_channel: Any,
    thread_id: str,
    account_id: int,
    text: str,
    config: PedagogyConfig | None = None,
) -> PedagogyTurn | None:
    """装配一轮教学回合; **返回 ``None`` 表示这一轮走普通聊天**(零教学改动)。

    返回 ``None`` 的四种情况, 每一种都是为了「不该教学时绝不打扰」:

    - ``PEDAGOGY_ENABLED=false``(全局降级开关);
    - 匿名(``account_id <= 0``): 循环要写 ``learner_explanation`` / ``mastery_ledger``,
      没有账号就没有证据归属 —— 教学技能本身也是 ``visibility: auth``(pedagogy-core §5);
    - 这个线程上没有载体, 且本轮没有明确的学习意图(闲聊就是闲聊);
    - 上一轮已 ``Closed``/``Escaped``, 且本轮没有提出新概念(不拿旧循环去套新问题)。

    ``raw_channel`` 是 checkpointer ``pedagogy`` 频道里的原始值(可能是 ``None``、坏数据);
    解析失败一律当作「没有载体」, 从 ``Pick`` 重新开始, 绝不让 ``/api/chat`` 变 500。
    """
    cfg = config or PedagogyConfig.from_settings()
    if not cfg.enabled or account_id <= 0:
        return None

    prior = LoopCarrier.from_channel(raw_channel)
    resume = prior is not None and not prior.state.terminal
    if not resume and not wants_to_learn(text):
        return None

    if resume and prior is not None:
        base = prior.state
        reading = read_turn(
            base, text, awaiting_escape=prior.awaiting_escape, config=cfg
        )
        turns = prior.turns + 1
    else:
        # 新循环: 从 Pick 起步。终态载体被刻意丢弃 —— 「闭合」是对某个概念的结论,
        # 换概念就是换一轮, 复用旧快照只会把上一个概念的字数与卡点算进来。
        base = PedagogyState(
            thread_id=thread_id, account_id=account_id, learner_id=account_id
        )
        reading = read_turn(base, text, awaiting_escape=False, config=cfg)
        turns = 1 if prior is None else prior.turns + 1

    step = tick(base, reading.event, config=cfg) if reading.event is not None else None
    state = step.state if step is not None else base
    carrier = LoopCarrier(
        state=state, awaiting_escape=reading.awaiting_escape, turns=turns
    )
    sink = PedagogySink()
    sink.carrier = carrier
    return PedagogyTurn(
        carrier=carrier,
        step=step,
        sink=sink,
        pre_events=_pre_events(step, reading, state, cfg),
    )


def _pre_events(
    step: LoopStep | None,
    reading: TurnReading,
    state: PedagogyState,
    cfg: PedagogyConfig,
) -> tuple[LearnEvent, ...]:
    """调模型之前就能确定的 ``learn`` 事件。

    顺序刻意固定为 **状态卡 -> 卡点 -> 护栏 -> 说明**: 前端按到达顺序渲染, 学习者
    应该先看到「现在在哪一步」, 再看到「哪里断了」, 最后才是内核对他这句话的解释。

    **软护栏(``action=log``, 目前只有 ``learner_talk_ratio``)在这里被刻意跳过。**
    此刻的系统字数还是上一轮的累计值, 本轮回复一个字都还没说; 中间件会在模型说完
    之后用当轮真实字数再报一次(``agent_pedagogy.aafter_model``)。两边都发, 学习者
    就会在同一轮里看到两张只有数字不同的「你说得太少」卡片。硬护栏
    (``truncate``/``suppress``/``confirm``)不受影响 —— 它们描述的是学习者这句话本身
    触发了什么, 必须立刻可见。``step.violations`` 原样保留, 落库/看板仍拿得到全量。
    """
    events: list[LearnEvent] = []
    if step is not None:
        events.append(
            LearnEvent.of_state(
                state,
                actions=tuple(action.value for action in step.actions),
                reason=step.reason,
                persona=recommend_persona(state),
                probes_left=remaining_probes(state.probes_used, cfg),
                loop_capped=state.loop_capped,
                notes=step.notes,
            )
        )
        events.extend(LearnEvent.of_gap(gap, state) for gap in state.open_gaps)
        events.extend(
            LearnEvent.of_guard(item, state)
            for item in step.violations
            if item.action is not GuardAction.LOG
        )
    if reading.note:
        events.append(LearnEvent.of_note(reading.note, state))
    return tuple(events)
