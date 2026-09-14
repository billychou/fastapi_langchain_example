"""学习证据的结构化单元 + 状态机事件/快照(见 ``docs/pedagogy-core.md`` §2、§4、§6)。

内核的一切输入输出都是**证据**, 证据必须结构化、可回放、可审计:
学习者原话 -> 命题拆解(claims) -> 卡点标注(gaps) -> 掌握度增量。

本模块只描述数据形状, 不含裁决逻辑(裁决在 ``loop.py``)、不含判分逻辑
(判分在 ``gaps.py``)、不落库(落库在 ``models/`` + ``services/learning_service.py``)。
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from app.pedagogy.states import TERMINAL_STATES, EventKind, FeynmanState

# --------------------------------------------------------------------------- 第一性原理: Atom

#: Atom 的种类决定 Why-chain 的下探终点: 前三种是**可停点**, ``derivable`` 必须继续下探。
AtomKind = Literal["definition", "axiom", "observable", "derivable"]

#: 可停点: 下探到这里就可以停止并记录深度与落点。
TERMINAL_ATOM_KINDS: frozenset[str] = frozenset({"definition", "axiom", "observable"})


class AtomSource(BaseModel):
    """Atom 的可核验来源。未溯源的 Atom 不得参与掌握度记账(vision §9 幻觉对策)。"""

    model_config = ConfigDict(frozen=True)

    title: str = ""
    locator: str = ""
    url: str = ""


class Atom(BaseModel):
    """知识原子: 一条可判真伪的最小命题 + 它在 Concept DAG 上的边。"""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1, max_length=191)
    concept_id: str = Field(default="", max_length=191)
    statement: str = Field(min_length=1)
    kind: AtomKind = "derivable"
    depends_on: tuple[str, ...] = ()
    entails: tuple[str, ...] = ()
    source: AtomSource | None = None
    verified: bool = False
    difficulty: int = Field(default=1, ge=1, le=5)
    misconceptions: tuple[str, ...] = ()

    @property
    def is_terminal(self) -> bool:
        """Why-chain 是否可以停在这个 Atom 上。"""
        return self.kind in TERMINAL_ATOM_KINDS


class AtomBundle(BaseModel):
    """``Return`` 状态补给学习者的最小必要 Atom 集合(带字数预算)。"""

    model_config = ConfigDict(frozen=True)

    atoms: tuple[Atom, ...] = ()
    max_chars: int = Field(default=300, ge=1)


# --------------------------------------------------------------------------- 卡点与判分

#: 四类断裂(产品核心 IP): 必须互斥且可操作 —— 每类都有检测信号与教学动作。
GapType = Literal["jargon_shield", "causal_gap", "broken_analogy", "no_boundary"]

#: claim 的判定状态。``vague`` / ``wrong`` 不是卡点, 但影响记账(见 pedagogy-core §3)。
ClaimStatus = Literal["supported", "unsupported", "wrong", "jargon_shield", "vague"]

#: ``closed`` 的门槛由内核按配置裁决, 不由判分器自说自话。
Verdict = Literal["closed", "not_closed"]


class Claim(BaseModel):
    """从学习者原话里拆出的一条命题。"""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1)
    atom_ref: str | None = None
    status: ClaimStatus = "unsupported"
    gap_type: GapType | None = None
    note: str = ""

    @model_validator(mode="after")
    def _supported_has_no_gap(self) -> Claim:
        if self.status == "supported" and self.gap_type is not None:
            raise ValueError("status=supported 的 claim 不应带 gap_type")
        return self


class MissingEdge(BaseModel):
    """卡点在 Concept DAG 上缺的那条边(``from`` entails ``to``)。"""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    from_atom: str = Field(alias="from")
    to_atom: str = Field(alias="to")


class GapItem(BaseModel):
    """一个卡点 = 类型 + 定位 + **下一步该问什么** + **该补哪个 Atom**。

    ``probe`` 必填: 诊断结果因此是**可执行的**, 而不是一句「理解不够深入」。
    """

    model_config = ConfigDict(frozen=True)

    type: GapType
    claim_id: str = ""
    missing_edge: MissingEdge | None = None
    probe: str = Field(min_length=1)
    supply: AtomBundle | None = None
    severity: int = Field(default=1, ge=1, le=3)


class GapScores(BaseModel):
    """判分 rubric 的三个维度, 各 0-1, 理由必填(由判分器输出)。"""

    model_config = ConfigDict(frozen=True)

    completeness: float = Field(default=0.0, ge=0.0, le=1.0)
    own_words: float = Field(default=0.0, ge=0.0, le=1.0)
    boundary_awareness: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""


class GapReport(BaseModel):
    """诊断结论(不是分数): 同一份 schema 在 ``Detect`` 与 ``Simplify`` 各用一次。"""

    model_config = ConfigDict(frozen=True)

    explanation_id: str = ""
    gaps: tuple[GapItem, ...] = ()
    scores: GapScores = Field(default_factory=GapScores)
    learner_talk_ratio: float = Field(default=0.0, ge=0.0, le=1.0)
    verdict: Verdict = "not_closed"
    #: 与已验证 Atom 冲突的 claim(``status=wrong``)。它们是误解库的原料,
    #: 且按 rubric 会阻断闭合 —— 所以必须随报告一起传给内核裁决。
    wrong_claim_ids: tuple[str, ...] = ()

    def gaps_of(self, *types: str) -> tuple[GapItem, ...]:
        return tuple(gap for gap in self.gaps if gap.type in types)


class ExplanationTurn(BaseModel):
    """学习者的一次自述(未加工原话 + 拆解结果), 是掌握度记账的唯一证据来源。"""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default="", max_length=64)
    thread_id: str = ""
    account_id: int = 0
    concept_id: str = ""
    loop_index: int = Field(default=0, ge=0)
    state: FeynmanState = FeynmanState.TEACH
    raw_text: str = ""
    claims: tuple[Claim, ...] = ()
    jargon_terms: tuple[str, ...] = ()
    analogy: str | None = None
    counterexample: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def chars(self) -> int:
        """自述字数(``min_explain_chars`` 护栏的口径)。

        刻意**派生而非存储**: 存两份就会互相矛盾, 而冻结模型无法在 ``after``
        校验器里回填, 所以这里直接算。落库时由 service 层写入同一口径。
        """
        return len(self.raw_text)


# --------------------------------------------------------------------------- 状态机事件


class _Event(BaseModel):
    """事件基类: ``system_chars`` 是这一轮系统说了多少字(用于 learner_talk_ratio 护栏)。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    system_chars: int = Field(default=0, ge=0)


class ConceptNamed(_Event):
    """``Pick -> Teach``: 概念已命名 + 前置 Atom 已列出。"""

    kind: Literal["concept_named"] = "concept_named"
    concept_id: str = Field(min_length=1, max_length=191)
    title: str = ""
    scope: str = ""
    prerequisites: tuple[str, ...] = ()


class ExplanationSubmitted(_Event):
    """``Teach -> Detect``: 学习者交出一份自述(字数不足则留在 ``Teach``)。"""

    kind: Literal["explanation_submitted"] = "explanation_submitted"
    chars: int = Field(ge=0)
    text: str = ""


class GapReported(_Event):
    """``Detect -> Return | Simplify``: 判分器交出诊断结论。"""

    kind: Literal["gap_reported"] = "gap_reported"
    report: GapReport


class SupplyCompleted(_Event):
    """``Return -> Teach``: 补给完成, 要求重讲。"""

    kind: Literal["supply_completed"] = "supply_completed"
    bundle: AtomBundle = Field(default_factory=AtomBundle)


class SimplifyJudged(_Event):
    """``Simplify -> Closed | Teach``: 简化重讲 + 自造类比 + 反例 + 迁移题的裁决。

    ``transfer_passed`` 是迁移题结果; 是否真的 ``closed`` 由内核按 rubric 阈值裁决,
    不接受调用方直接塞一个 bool。
    """

    kind: Literal["simplify_judged"] = "simplify_judged"
    report: GapReport
    transfer_passed: bool = False


class EscapeRequested(_Event):
    """逃生舱: 学习者要求直答。必须 ``confirmed`` 才真的直答, 且直答不记掌握度。"""

    kind: Literal["escape_requested"] = "escape_requested"
    confirmed: bool = False


LoopEvent = Annotated[
    ConceptNamed
    | ExplanationSubmitted
    | GapReported
    | SupplyCompleted
    | SimplifyJudged
    | EscapeRequested,
    Field(discriminator="kind"),
]

#: 事件类型 -> 事件类, 便于中间件按 ``kind`` 反序列化。
EVENT_TYPES: dict[EventKind, type[_Event]] = {
    "concept_named": ConceptNamed,
    "explanation_submitted": ExplanationSubmitted,
    "gap_reported": GapReported,
    "supply_completed": SupplyCompleted,
    "simplify_judged": SimplifyJudged,
    "escape_requested": EscapeRequested,
}


# --------------------------------------------------------------------------- 状态快照


class PedagogyState(BaseModel):
    """一轮费曼循环的完整快照。

    **不含时间戳**: ``tick()`` 因此是确定性纯函数, 同一输入永远得到同一输出;
    时间由持久化层(``services/learning_service.py``)在写库时补。
    """

    model_config = ConfigDict(frozen=True)

    thread_id: str = ""
    account_id: int = 0
    learner_id: int = 0
    concept_id: str = ""
    state: FeynmanState = FeynmanState.PICK
    #: 进入 ``Teach`` 的次数(1 起), 上限 ``PedagogyConfig.max_loops``。
    loop_index: int = 0
    prerequisites: tuple[str, ...] = ()
    open_gaps: tuple[GapItem, ...] = ()
    probes_used: int = 0
    learner_chars: int = Field(default=0, ge=0)
    system_chars: int = Field(default=0, ge=0)
    last_scores: GapScores | None = None
    loop_capped: bool = False

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def talk_ratio(self) -> float:
        """学习者发言占比(护栏 ``learner_talk_ratio`` 的口径)。"""
        total = self.learner_chars + self.system_chars
        return (self.learner_chars / total) if total else 0.0
