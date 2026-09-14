"""把一轮 HTTP 请求翻译成一个内核事件(**确定性, 不调 LLM**)。

内核的裁决权在 :func:`app.pedagogy.loop.tick`, 但 tick 需要一个 ``LoopEvent``。
事件从哪来分两条路:

- **学习者侧**(本模块): 命名概念、交出自述、请求直答 —— 这三件事可以从请求文本
  直接判定, 不需要模型参与, 因此在路由里同步完成, 模型这一轮就能看到新状态;
- **教练侧**(``additional_kwargs["pedagogy_events"]``, 由 ``agent_pedagogy`` 中间件解析):
  诊断结论(``gap_reported``)、补给完成(``supply_completed``)、简化裁决
  (``simplify_judged``) —— 这些必须基于对讲解的理解, P0 由 mock 脚本产生,
  真实模型走结构化输出(P1)。

刻意**不做**的事: 不在这里判分、不在这里推断「他到底懂没懂」。判分是
:mod:`app.pedagogy.gaps` 的职责, 而且必须可注入、可评估(Cohen's κ)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from app.pedagogy.policy import PedagogyConfig
from app.pedagogy.schemas import (
    ConceptNamed,
    EscapeRequested,
    ExplanationSubmitted,
    LoopEvent,
    PedagogyState,
)
from app.pedagogy.states import FeynmanState

#: 本轮意图(可观测性用; 也方便测试断言「读对了没有」)。
TurnIntent = Literal["start", "explain", "escape", "confirm_escape", "deny_escape", "none"]

# 「我想搞懂 X」/「教我 X」/「帮我理解一下 X」/「给我讲讲 X」
_LEARN_REQUEST = re.compile(
    r"(?:(?:我想|我要|我想要|帮我|请|麻烦|能不能|可不可以|想)"
    r"\s*(?:搞懂|弄懂|学懂|学会|理解|掌握|学|研究)"
    # 动词分支必须**长词在前**: 正则的交替是「第一个能匹配的就赢」, 写成 `给我讲|给我讲讲`
    # 时 "给我讲讲什么是闭包" 会在 `给我讲` 处停下, 把剩下的 "讲什么是闭包" 当成概念名。
    r"|(?:教我|给我讲讲|给我讲|讲解一下|讲一讲|讲讲|说一下|说说))"
    r"\s*(?:一下|下)?\s*[:：]?\s*(?P<concept>[^，。；、？?！!\n]{2,40})"
)
# 「检验我是不是真懂了 X」/「考考我 X」
_CHECK_REQUEST = re.compile(
    r"(?:检验|考考|测试|检查)\s*我?\s*(?:一下|下)?\s*我?\s*(?:是不是|是否)?\s*"
    r"(?:真的?懂了|真懂了|懂了|理解了|掌握了)?\s*[:：]?\s*"
    r"(?P<concept>[^，。；、？?！!\n]{2,40})"
)
CONCEPT_PATTERNS: tuple[re.Pattern[str], ...] = (_LEARN_REQUEST, _CHECK_REQUEST)

#: 逃生舱: 学习者要求直答(``escape_hatch`` 护栏的入口)。
ESCAPE_RE = re.compile(
    r"(?:直接(?:告诉我|说答案|给答案|公布答案|讲答案|给结论)"
    r"|别(?:再)?问了|不用问了|我不想(?:讲|说)了?|讲不动了|说不出来|我放弃|跳过(?:吧|这[一步题]*))"
)
#: 对「要不要直答」的肯定回答(仅在上一轮已问过确认时才生效)。
CONFIRM_RE = re.compile(
    r"(?:^|[\s，,。！!])(?:是的?|对的?|嗯+|好的?|行|可以|确认|确定|没问题|继续|要)(?:$|[\s，,。！!、])"
)
#: 对「要不要直答」的否定回答: 撤回确认, 回到原状态继续循环。
DENY_RE = re.compile(r"(?:不要|不用|别|算了|取消|我还是自己(?:讲|说)|继续(?:讲|问|循环))")

#: 概念名两端的包裹符号: 学习者常把概念写在引号/书名号里(「闭包」/《熵》)。
_WRAPPER_CHARS = "「」\"'《》()（） "
#: 概念名的首/尾语气词兜底(正则已经吃掉大部分, 这里处理漏网的)。
#: 「是什么」在尾部、「什么是」在头部: 两种疑问句式都要还原成裸概念名,
#: 否则同一节课会因为提问措辞不同而记出两个 concept_id(账本碎裂, 见 extract_concept 文档)。
_CONCEPT_NOISE = ("一下", "下", "吧", "啊", "呢", "了", "是什么", "指什么")
_CONCEPT_LEAD_NOISE = ("我", "一下", "下", "就是", "所谓的")
#: 疑问句式前缀: 单独一轮剥离, 允许把概念名剥到只剩 1 个字(「什么叫熵」->「熵」)。
#: 与 ``_CONCEPT_LEAD_NOISE`` 分开是因为后者带「至少留 2 字」的保护, 而疑问前缀本身
#: 就有 2-3 字, 那条保护会让单字概念永远剥不干净。
_QUESTION_LEAD = ("什么是", "什么叫", "啥是", "何谓")


def extract_concept(text: str) -> str | None:
    """从一句话里规则化地抽出概念名; 抽不出返回 ``None``。

    规则抽取(而不是让模型抽)的理由: 概念命名决定了这一轮循环记账到哪个 ``concept_id``,
    它是**证据的一部分**; P0 宁可覆盖面窄(只认明确的学习意图句式), 也不要一个
    每次都不一样的名字 —— 名字漂移会让掌握度账本碎成一地。
    """
    if not isinstance(text, str) or not text:
        # 非字符串(脏输入)按「认不出」处理: 判读失败不该让 /api/chat 变 500。
        return None
    for pattern in CONCEPT_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        raw = re.sub(r"\s+", " ", match.group("concept")).strip()
        cleaned = _strip_noise(_strip_wrappers(raw), lead=True)
        # 疑问前缀剥掉之后可能又露出一层引号(「什么是「闭包」」), 所以两端都要再剥一次。
        concept = _strip_wrappers(_strip_question_lead(cleaned))
        # 概念名至少 2 字; 唯一的例外是「确实剥掉了疑问句式」的单字概念(什么叫熵 -> 熵)。
        # 这条门槛不是为了好看: concept_id 是掌握度账本的主键之一, 宁可这一轮不认,
        # 也不要记下一个下次对不上的名字。
        if len(concept) >= 2 or (len(concept) == 1 and len(cleaned) >= 3):
            return concept
    return None


def _strip_wrappers(concept: str) -> str:
    """剥掉概念名两端的引号/书名号/括号(不影响名字内部的标点)。"""
    return concept.strip(_WRAPPER_CHARS)


def _strip_question_lead(concept: str) -> str:
    """剥掉疑问句式前缀; 剥完为空就原样返回(「什么是」本身就是概念名时不误伤)。"""
    for noise in _QUESTION_LEAD:
        if concept.startswith(noise) and len(concept) > len(noise):
            return concept[len(noise) :].strip()
    return concept


def _strip_noise(concept: str, *, lead: bool = False) -> str:
    """去掉概念名首尾的语气词; 迭代到不动点(每轮要么严格变短要么退出, 必然终止)。"""
    changed = True
    while changed:
        changed = False
        for noise in _CONCEPT_NOISE:
            if concept.endswith(noise) and len(concept) > len(noise) + 1:
                concept = concept[: -len(noise)].strip()
                changed = True
        if lead:
            for noise in _CONCEPT_LEAD_NOISE:
                # 首部语气词剥到只剩 1 字也照剥: 留下「我熵」这种带人称的概念名会污染
                # 账本主键, 而 1 字概念本来就会被 extract_concept 的门槛挡住(宁可这一轮
                # 不认)。尾部保留 +1 的保护, 否则「一下」会把「下」这类真概念名吃掉。
                if concept.startswith(noise) and len(concept) > len(noise):
                    concept = concept[len(noise) :].strip()
                    changed = True
    return concept


def concept_slug(title: str) -> str:
    """概念名 -> 稳定 slug(``PedagogyState.concept_id``, 也是 ``learning_concept.slug``)。

    中英文都保留(不做拼音/翻译), 只做小写、空白转 ``-``、去标点、截断 64 字。
    稳定压倒好看: 同一句话必须永远得到同一个 slug, 否则账本对不上。
    """
    slug = re.sub(r"\s+", "-", title.strip().lower())
    slug = re.sub(r"[^\w-]", "", slug, flags=re.UNICODE)
    return slug[:64] or "concept"


def wants_to_learn(text: str) -> bool:
    """这句话是否是一次「开启学习循环」的意图(路由用它决定是否下发教学状态)。"""
    return extract_concept(text) is not None


def wants_direct_answer(text: str) -> bool:
    return isinstance(text, str) and bool(ESCAPE_RE.search(text))


@dataclass(frozen=True, slots=True)
class TurnReading:
    """一轮请求文本的判读结果。

    ``awaiting_escape`` 是**跨轮**事实(上一轮有没有就直答要过确认), 由调用方存回
    :class:`app.pedagogy.events.LoopCarrier`; ``note`` 会作为 ``learn.note`` 事件下发,
    让前端能解释「为什么这一轮状态没动」。
    """

    event: LoopEvent | None = None
    intent: TurnIntent = "none"
    awaiting_escape: bool = False
    concept: str = ""
    note: str = ""


def read_turn(
    state: PedagogyState,
    text: str,
    *,
    awaiting_escape: bool = False,
    config: PedagogyConfig | None = None,
) -> TurnReading:
    """判读学习者这一轮说了什么, 产出**至多一个**内核事件。

    优先级(刻意固定, 便于测试与解释):

    1. 已就直答要过确认 -> 肯定则 ``EscapeRequested(confirmed=True)``, 否定则撤回;
    2. 要求直答 -> ``EscapeRequested(confirmed=False)``(``escape_hatch`` 护栏会拦下未确认的直答);
    3. ``Teach`` -> 任何发言都算一次自述(字数达不达标由内核判, 不在这里猜);
    4. ``Pick`` -> 能抽出概念名才 ``ConceptNamed``;
    5. 其余状态 -> 无事件(``Detect``/``Return``/``Simplify`` 的推进需要判分器, 属教练侧事件)。
    """
    cfg = config or PedagogyConfig.from_settings()
    text = text.strip() if isinstance(text, str) else ""
    if not text:
        return TurnReading(note="本轮没有学习者发言, 状态不变")

    if awaiting_escape:
        if DENY_RE.search(text):
            return TurnReading(
                intent="deny_escape",
                awaiting_escape=False,
                note="已撤回直答请求, 继续循环",
            )
        if CONFIRM_RE.search(text):
            return TurnReading(
                event=EscapeRequested(confirmed=True, system_chars=0),
                intent="confirm_escape",
                awaiting_escape=False,
            )

    if wants_direct_answer(text):
        return TurnReading(
            event=EscapeRequested(confirmed=False, system_chars=0),
            intent="escape",
            awaiting_escape=True,
            note="检测到直答请求; escape_hatch 护栏要求先确认(直答不计掌握度)",
        )

    if state.state is FeynmanState.TEACH:
        return TurnReading(
            event=ExplanationSubmitted(
                chars=len(text), text=text[: cfg.min_explain_chars * 20], system_chars=0
            ),
            intent="explain",
            awaiting_escape=awaiting_escape,
        )

    if state.state is FeynmanState.PICK:
        concept = extract_concept(text)
        if concept is None:
            return TurnReading(
                intent="none",
                awaiting_escape=awaiting_escape,
                note="还没命名概念; 说「我想搞懂 X」即可开始一轮费曼循环",
            )
        return TurnReading(
            event=ConceptNamed(
                concept_id=concept_slug(concept), title=concept, system_chars=0
            ),
            intent="start",
            awaiting_escape=False,
            concept=concept,
        )

    # Detect / Return / Simplify: 推进需要教练侧事件(判分/补给/迁移题裁决)
    return TurnReading(
        intent="none",
        awaiting_escape=awaiting_escape,
        note=(
            f"当前处于 {state.state}, 这一轮的发言要经判分器产出诊断后才能推进状态机"
            "(教练侧事件, 见 app/pedagogy/turn.py 模块文档)"
        ),
    )
