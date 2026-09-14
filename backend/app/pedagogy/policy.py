"""护栏与阈值配置(见 ``docs/pedagogy-core.md`` §1.2)。

五条护栏都必须**可自动断言** —— 它们不是提示词里的劝告, 而是内核里的硬拦截:

- ``learner_talk_ratio``: 学习者发言占比 < 阈值 -> 记为教学质量事件, 触发策略降级;
- ``no_direct_answer``: ``Detect``/``Return`` 下系统输出含完整解题步骤 -> 截断改写;
- ``probe_budget``: 单轮追问超预算 -> 第 N+1 个追问被抑制;
- ``monologue_cap``: 单条系统消息超上限 -> 超出部分转为附件卡片;
- ``escape_hatch``: 学习者要求直答 -> 必须先确认, 且直答不记掌握度。

阈值一律来自 :class:`PedagogyConfig`(由 ``PEDAGOGY_*`` 环境变量装配), 不写死在代码里,
这样调参不需要改代码(roadmap §0 原则 4)。所有 ``enforce_*`` 都是纯函数,
返回 ``(处理后的文本, 违规记录)``, 便于在中间件里直接接管模型输出。
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings, get_settings
from app.pedagogy.states import FeynmanState


class Guard(StrEnum):
    """护栏名(与 pedagogy-core §1.2 表格一一对应)。"""

    LEARNER_TALK_RATIO = "learner_talk_ratio"
    NO_DIRECT_ANSWER = "no_direct_answer"
    PROBE_BUDGET = "probe_budget"
    MONOLOGUE_CAP = "monologue_cap"
    ESCAPE_HATCH = "escape_hatch"


class GuardAction(StrEnum):
    """违规后内核采取的动作。"""

    LOG = "log"  # 只记录, 不阻断(软护栏)
    TRUNCATE = "truncate"  # 截断并改写
    SUPPRESS = "suppress"  # 抑制超预算的部分
    CONFIRM = "confirm"  # 需要学习者二次确认

#: 这些状态下禁止给出完整解法(应该指出缺哪个 Atom)。
NO_DIRECT_ANSWER_STATES: frozenset[FeynmanState] = frozenset(
    {FeynmanState.DETECT, FeynmanState.RETURN}
)

#: 截断后补上的改写提示(让模型下一轮知道该怎么说)。
DIRECT_ANSWER_REWRITE_HINT = "（内核拦截：此处应指出「缺哪个 Atom」，而不是给出完整解题步骤。）"

# 解题步骤信号: 行首编号 / 「第 N 步」/「步骤 N」
_STEP_MARKER = re.compile(
    r"^[ \t]*(?:\d+[.、)）]|[（(]\d+[)）]|第\s*[0-9一二三四五六七八九十]+\s*步|步骤\s*\d+)",
    re.MULTILINE,
)
# 直答信号: 「答案：」「答案是 42」「最终结果」。
# 「结果是」刻意不算 —— 它在追问里太常见("你刚才说的结果是…"), 会造成误伤。
_ANSWER_MARKER = re.compile(r"答案\s*[:：是]|结果\s*[:：]|最终(?:答案|结果)")


class PedagogyConfig(BaseModel):
    """教学内核的全部阈值。默认值即 pedagogy-core 里写明的数字。"""

    model_config = ConfigDict(frozen=True)

    enabled: bool = True
    min_explain_chars: int = Field(default=80, ge=1)
    max_loops: int = Field(default=3, ge=1)
    max_probes_per_turn: int = Field(default=3, ge=1)
    monologue_cap_chars: int = Field(default=800, ge=1)
    return_max_chars: int = Field(default=600, ge=1)
    min_learner_talk_ratio: float = Field(default=0.5, ge=0.0, le=1.0)
    min_solution_steps: int = Field(default=3, ge=2)
    min_completeness: float = Field(default=0.8, ge=0.0, le=1.0)
    min_own_words: float = Field(default=0.6, ge=0.0, le=1.0)
    why_chain_max_depth: int = Field(default=5, ge=1)
    mastery_alpha: float = Field(default=0.35, gt=0.0, le=1.0)
    mastery_coldstart_tau_days: float = Field(default=7.0, gt=0.0)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> PedagogyConfig:
        """从 ``Settings`` 装配(``PEDAGOGY_*`` 环境变量)。"""
        cfg = settings or get_settings()
        return cls(
            enabled=cfg.pedagogy_enabled,
            min_explain_chars=cfg.pedagogy_min_explain_chars,
            max_loops=cfg.pedagogy_max_loops,
            max_probes_per_turn=cfg.pedagogy_max_probes_per_turn,
            monologue_cap_chars=cfg.pedagogy_monologue_cap_chars,
            return_max_chars=cfg.pedagogy_return_max_chars,
            min_learner_talk_ratio=cfg.pedagogy_min_learner_talk_ratio,
            min_solution_steps=cfg.pedagogy_min_solution_steps,
            min_completeness=cfg.pedagogy_min_completeness,
            min_own_words=cfg.pedagogy_min_own_words,
            why_chain_max_depth=cfg.pedagogy_why_chain_max_depth,
            mastery_alpha=cfg.pedagogy_mastery_alpha,
            mastery_coldstart_tau_days=cfg.pedagogy_mastery_coldstart_tau_days,
        )


class Violation(BaseModel):
    """一次护栏触发记录(可落库、可看板化, 是教学质量指标的数据源)。"""

    model_config = ConfigDict(frozen=True)

    guard: Guard
    action: GuardAction
    detail: str = ""


# --------------------------------------------------------------------------- 护栏实现


def talk_ratio(learner_chars: int, system_chars: int) -> float:
    """学习者发言占比; 双方都没说话时记 0.0。"""
    total = learner_chars + system_chars
    return (learner_chars / total) if total > 0 else 0.0


def check_talk_ratio(
    learner_chars: int, system_chars: int, config: PedagogyConfig
) -> Violation | None:
    """``learner_talk_ratio``: 低于阈值只**记录**(软护栏), 不阻断转移。"""
    ratio = talk_ratio(learner_chars, system_chars)
    if ratio >= config.min_learner_talk_ratio:
        return None
    return Violation(
        guard=Guard.LEARNER_TALK_RATIO,
        action=GuardAction.LOG,
        detail=(
            f"学习者发言占比 {ratio:.2f} < {config.min_learner_talk_ratio:.2f}"
            f"(learner={learner_chars}, system={system_chars}); 下一轮应少讲多问"
        ),
    )


def find_solution_start(text: str, min_steps: int) -> int | None:
    """定位「完整解题步骤/直接答案」的起点; 没有则返回 ``None``。

    判定信号有两类, 取更靠前的那个:
    1. 出现 ``min_steps`` 个及以上的步骤编号(行首 ``1.`` / ``第 2 步`` / ``步骤3``);
    2. 出现直答标记(``答案：`` / ``最终结果``)。
    """
    if not text:
        return None
    candidates: list[int] = []
    steps = list(_STEP_MARKER.finditer(text))
    if len(steps) >= min_steps:
        candidates.append(steps[0].start())
    answer = _ANSWER_MARKER.search(text)
    if answer:
        candidates.append(answer.start())
    return min(candidates) if candidates else None


def enforce_no_direct_answer(
    state: FeynmanState, text: str, config: PedagogyConfig
) -> tuple[str, Violation | None]:
    """``no_direct_answer``: 在 ``Detect``/``Return`` 下截断完整解法并改写为「缺哪个 Atom」。

    其他状态(如 ``Closed`` 后的补充讲解、或学习者已确认的逃生舱直答)不拦截。
    """
    if state not in NO_DIRECT_ANSWER_STATES or not text:
        return text, None
    cut = find_solution_start(text, config.min_solution_steps)
    if cut is None:
        return text, None
    kept = text[:cut].rstrip()
    rewritten = f"{kept}\n\n{DIRECT_ANSWER_REWRITE_HINT}" if kept else DIRECT_ANSWER_REWRITE_HINT
    return rewritten, Violation(
        guard=Guard.NO_DIRECT_ANSWER,
        action=GuardAction.TRUNCATE,
        detail=f"在第 {cut} 字处截断完整解题步骤, 已改写为 Atom 缺口提示",
    )


def enforce_probe_budget(
    probes: tuple[str, ...] | list[str], config: PedagogyConfig
) -> tuple[tuple[str, ...], Violation | None]:
    """``probe_budget``: 单轮追问上限, 超出的被抑制(转为补给或结论)。"""
    allowed = tuple(probes[: config.max_probes_per_turn])
    dropped = len(probes) - len(allowed)
    if dropped <= 0:
        return allowed, None
    return allowed, Violation(
        guard=Guard.PROBE_BUDGET,
        action=GuardAction.SUPPRESS,
        detail=f"单轮追问 {len(probes)} 个 > 预算 {config.max_probes_per_turn}, 已抑制 {dropped} 个",
    )


def enforce_monologue_cap(
    text: str, config: PedagogyConfig
) -> tuple[str, str, Violation | None]:
    """``monologue_cap``: 单条系统消息字数上限, 超出部分转成附件卡片而不塞进对话。

    返回 ``(保留在对话里的文本, 溢出内容, 违规记录)``。优先在最后一个换行处切,
    避免把一句话拦腰截断。
    """
    if len(text) <= config.monologue_cap_chars:
        return text, "", None
    window = text[: config.monologue_cap_chars]
    cut = window.rfind("\n")
    if cut < config.monologue_cap_chars // 2:  # 换行点太靠前就按硬上限切
        cut = config.monologue_cap_chars
    kept, overflow = text[:cut].rstrip(), text[cut:].strip()
    return kept, overflow, Violation(
        guard=Guard.MONOLOGUE_CAP,
        action=GuardAction.TRUNCATE,
        detail=(
            f"系统消息 {len(text)} 字 > 上限 {config.monologue_cap_chars}, "
            f"溢出 {len(overflow)} 字转为附件卡片"
        ),
    )


def escape_violation(confirmed: bool) -> Violation | None:
    """``escape_hatch``: 未确认的直答请求必须先确认。"""
    if confirmed:
        return None
    return Violation(
        guard=Guard.ESCAPE_HATCH,
        action=GuardAction.CONFIRM,
        detail="学习者请求直答但未确认; 直答不计入掌握度, 可事后补测",
    )
