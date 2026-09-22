"""学习证据 API 的请求/响应模型(P0 #7)。

三条贯穿全文件的取舍, 都是为了让「HTTP 契约」与「内核契约」不分叉:

1. **直接复用内核模型**: ``claims`` / ``gap_report`` 用 :class:`app.pedagogy.schemas.Claim`
   与 :class:`~app.pedagogy.schemas.GapReport`, ``evidence.kind`` 用
   :data:`app.pedagogy.mastery.EvidenceKind`, ``state`` 用
   :class:`app.pedagogy.states.FeynmanState`。再写一份「API 版卡点」就会有两处真值 ——
   判分器加一类卡点时, 忘改 API schema 的话前端会静默丢掉它, 而测试全绿。
   代价是这些模型是 frozen + 严格校验的, 请求体不合法会得到 422(这是想要的)。
2. **对外只暴露稳定字符串键**: 概念用 ``slug``、原子用 ``atom_key``, **不暴露自增主键**。
   内核只认字符串(``Atom.id`` ↔ ``atom_key``), 主键翻译发生在 service 层;
   于是「换库/重排 ID」不影响任何已发出去的 URL, 也不会让学习者收藏的链接失效。
3. **不记账也要说清楚**: ``MasteryDelta.credited=false`` + ``reason`` 原样返回
   (读直答、放弃、``verified=false`` 的 Atom)。悄悄丢掉一条证据, 家长/教研看到的就是
   「我明明闭合了, 掌握度没动」, 而这恰恰是最需要解释的一种情况。

权限点与 ``migrations/005_learning.sql`` 的种子逐字对应(由 ``tests/test_migrations.py``
守卫那张表), 所以这里的常量改一处就会与迁移不一致 —— 别改, 要改就一起改。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.pedagogy.mastery import EvidenceKind
from app.pedagogy.schemas import AtomKind, Claim, GapReport
from app.pedagogy.states import FeynmanState

#: 与迁移 ``005_learning.sql`` 播种的权限点逐字一致。
PERM_SELF_READ = "learning:self:read"
PERM_EXPLANATION_WRITE = "learning:explanation:write"
PERM_REPORT_READ = "learning:report:read"
PERM_ATOM_ADMIN = "learning:atom:admin"

#: 概念 slug: 允许中英文与 ``-``/``_``(``app.pedagogy.turn.concept_slug`` 的产物),
#: 只禁空白与标点 —— slug 是掌握度账本的主键之一, 「稳定压倒好看」。
SLUG_PATTERN = r"^[\w-]{1,128}$"
#: 知识原子的稳定键(与 ``learning_atom.atom_key`` 同宽); ``\w`` 已含中英文。
ATOM_KEY_PATTERN = r"^[\w.:-]{1,191}$"

#: 依赖边关系词表(与 ``app.models.learning.ATOM_RELATIONS`` 一致)。
AtomRelation = Literal["depends_on", "entails", "contradicts"]


# --------------------------------------------------------------------------- 知识图


class AtomSourceIn(BaseModel):
    """Atom 的来源(可核验性)。未溯源的 Atom 不得记账, 所以来源是这张表的重点。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="", max_length=255)
    locator: str = Field(default="", max_length=255, description="章节/页码/时间戳")
    url: str = Field(default="", max_length=1024)


class AtomIn(BaseModel):
    """写入一个知识原子(``POST /learning/concepts`` 的子结构)。"""

    model_config = ConfigDict(extra="forbid")

    atom_key: str = Field(pattern=ATOM_KEY_PATTERN, description="稳定标识, 形如 atom:closure-scope")
    statement: str = Field(min_length=1, description="一条可判真伪的最小命题")
    kind: AtomKind = Field(default="derivable", description="definition/axiom/observable 是 Why-chain 可停点")
    source: AtomSourceIn | None = None
    verified: bool = Field(default=False, description="false 时不参与掌握度记账")
    difficulty: int = Field(default=1, ge=1, le=5)
    depends_on: tuple[str, ...] = Field(default=(), description="本 Atom 依赖的 atom_key")
    entails: tuple[str, ...] = Field(default=(), description="本 Atom 蕴含的 atom_key")

    @model_validator(mode="after")
    def _no_self_edge(self) -> AtomIn:
        """自环会让 Why-chain 原地打转, 也会让概念掌握度的权重分母失真。"""
        loops = {key for key in (*self.depends_on, *self.entails) if key == self.atom_key}
        if loops:
            raise ValueError(f"atom {self.atom_key} 不能依赖/蕴含自己")
        return self


class ConceptUpsertRequest(BaseModel):
    """建立或绑定一个概念, 并顺带写入它的前置 Atom 子图。

    ``slug`` 缺省时由 ``title`` 经 :func:`app.pedagogy.turn.concept_slug` 生成 ——
    与聊天里「我想搞懂闭包」得到的 ``concept_id`` 同一个函数, 否则两条路径
    会为同一个概念各建一行, 掌握度账本就此分叉。
    """

    model_config = ConfigDict(extra="forbid")

    slug: str | None = Field(default=None, pattern=SLUG_PATTERN, max_length=128)
    title: str = Field(min_length=1, max_length=255)
    subject: str | None = Field(default=None, max_length=64, description="学科标签, 仅用于筛选")
    scope: str | None = Field(default=None, description="范围边界: 明确不讨论什么(no_boundary 的判据)")
    summary: str | None = None
    atoms: tuple[AtomIn, ...] = Field(default=(), description="要写入/更新的知识原子")
    replace_atoms: bool = Field(
        default=False,
        description="true 时删除本次未出现的旧 Atom(整图替换); 默认增量 upsert",
    )


class ConceptOut(BaseModel):
    id: int = Field(description="数据库主键, 仅供排查; 对外引用一律用 slug")
    slug: str
    title: str
    subject: str | None = None
    scope: str | None = None
    summary: str | None = None
    created_at: datetime | None = None


class AtomOut(BaseModel):
    atom_key: str
    statement: str
    kind: str
    verified: bool
    difficulty: int
    source_title: str | None = None
    source_locator: str | None = None
    source_url: str | None = None


class AtomEdgeOut(BaseModel):
    from_atom: str
    to_atom: str
    relation: AtomRelation
    weight: float


class ConceptUpsertResult(BaseModel):
    """``POST /learning/concepts`` 的结果: 概念 + 这次到底改了什么。

    ``created`` / ``atom_keys_created`` 让调用者能区分「新建」与「幂等重放」——
    教研脚本重复跑不该产生第二份证据, 也不该让人以为写入失败了。
    """

    concept: ConceptOut
    created: bool = Field(description="true=本次新建概念行, false=已存在(幂等绑定)")
    atoms: tuple[AtomOut, ...] = ()
    edges: tuple[AtomEdgeOut, ...] = ()
    atom_keys_created: tuple[str, ...] = ()
    atom_keys_updated: tuple[str, ...] = ()
    atom_keys_removed: tuple[str, ...] = Field(default=(), description="仅 replace_atoms=true 时非空")


class AtomMasteryOut(BaseModel):
    """概念图上一个 Atom 的**我的**掌握度(他人数据永不出现在这个响应里)。"""

    atom_key: str
    mastery: float = Field(ge=0.0, le=1.0)
    weight: float = Field(gt=0.0, description="概念掌握度的分母权重(取自依赖边)")
    evidence_count: int = 0
    review_stage: int = 0
    interval_days: int = 0
    next_review_at: datetime | None = None
    #: ``None`` = 这个 Atom 我还没有账目(没记过证据), 与「掌握度 0」是两件事。
    recorded: bool = Field(default=False)


class ConceptMasteryOut(BaseModel):
    """按概念聚合的加权掌握度 ``M = Σ m·w / Σ w``。

    ``atoms`` 与 ``recorded`` 必须分列: 聚合口径是「覆盖整个概念」—— 没有账目的 Atom
    以 m=0 参与分母, 于是 ``atoms`` 是概念下的 Atom 总数, ``recorded`` 才是我学过的个数。
    只给一个数的话, 「闭合 1/5」与「闭合 5/5」会算出同一个 100%。
    """

    concept_slug: str
    concept_title: str
    mastery: float = Field(ge=0.0, le=1.0)
    atoms: int = Field(description="参与聚合的 Atom 数(含未记账的, 以 m=0 计入)")
    recorded: int = Field(default=0, description="其中我已有账目(记过证据)的 Atom 数")
    due: int = Field(default=0, description="其中已到复习期的数量")


class ConceptGraphOut(BaseModel):
    """概念子图: Atom + 边 + 我的掌握度(前端概念图页与教练的上下文同源)。"""

    concept: ConceptOut
    atoms: tuple[AtomOut, ...] = ()
    edges: tuple[AtomEdgeOut, ...] = ()
    mastery: tuple[AtomMasteryOut, ...] = ()
    concept_mastery: float = Field(default=0.0, ge=0.0, le=1.0)


# --------------------------------------------------------------------------- 学习证据


class EvidenceIn(BaseModel):
    """随自述一起提交的一条掌握度证据(记账的唯一 HTTP 入口)。

    ``atom_keys`` 必须是本概念下真实存在的 Atom; 不存在的键会被忽略并在响应的
    ``skipped_atom_keys`` 里点名 —— 静默忽略会让「判分器给了个错 key」永远查不出来。
    """

    model_config = ConfigDict(extra="forbid")

    kind: EvidenceKind = Field(default="closed_explanation")
    atom_keys: tuple[str, ...] = Field(min_length=1, max_length=64)
    depth: int = Field(default=0, ge=0, description="最近一次 Why-chain 下探深度(权重修正用)")


class ExplanationCreate(BaseModel):
    """提交一份自述(费曼循环 ``Teach`` 的原始产物)。

    ``claims`` / ``gap_report`` 可以留空: 留空表示「只存原话, 判分稍后补」——
    判分器换 rubric 时拿 ``raw_text`` 重跑正是靠这一点, 所以原话与结论分列存储。
    """

    model_config = ConfigDict(extra="forbid")

    concept_slug: str = Field(pattern=SLUG_PATTERN, max_length=128)
    raw_text: str = Field(min_length=1, description="学习者原话, 不加工")
    thread_id: str | None = Field(default=None, max_length=64, description="关联 LangGraph thread(L1)")
    loop_index: int = Field(default=1, ge=1, le=32)
    state: FeynmanState = Field(default=FeynmanState.TEACH)
    claims: tuple[Claim, ...] = ()
    gap_report: GapReport | None = None
    learner_talk_ratio: float | None = Field(default=None, ge=0.0, le=1.0)
    evidence: EvidenceIn | None = None


class MasteryDeltaOut(BaseModel):
    """一次记账的结果, 与 ``learn.mastery`` SSE 载荷同形(前端一套渲染逻辑)。"""

    atom_key: str
    before: float
    decayed: float = Field(description="按 Δt 衰减到当下、尚未加增量时的值")
    after: float
    weight: float
    credited: bool = Field(description="false=不记账(读直答/放弃/Atom 未溯源/w=0)")
    reason: str = ""


class ExplanationOut(BaseModel):
    """一条讲解证据的完整视图(``GET /learning/explanations/{id}``)。"""

    id: int
    concept_slug: str
    thread_id: str | None = None
    loop_index: int
    state: str
    raw_text: str
    chars: int = Field(description="原话字数(MIN_EXPLAIN_CHARS 的口径)")
    claims: tuple[dict, ...] = ()
    gap_report: dict | None = None
    learner_talk_ratio: float | None = None
    created_at: datetime | None = None
    mastery: tuple[MasteryDeltaOut, ...] = Field(
        default=(), description="仅 POST 返回: 本次提交带来的掌握度增量"
    )
    skipped_atom_keys: tuple[str, ...] = Field(
        default=(), description="仅 POST 返回: evidence 里不属于该概念的 atom_key"
    )


class ExplanationItem(BaseModel):
    """列表项: 不带原话全文(列表页只要索引), 详情走 ``/{id}``。"""

    id: int
    concept_slug: str
    loop_index: int
    state: str
    chars: int
    gap_types: tuple[str, ...] = ()
    verdict: str | None = Field(default=None, description="closed / not_closed(判分器结论)")
    created_at: datetime | None = None


class ExplanationListOut(BaseModel):
    count: int
    items: tuple[ExplanationItem, ...] = ()


class MasteryRowOut(BaseModel):
    """掌握度账本的一行(每个数字都追得到证据: atom_key + evidence_count + 时间戳)。"""

    atom_key: str
    statement: str
    concept_slug: str
    concept_title: str
    verified: bool
    mastery: float
    weight: float
    evidence_count: int
    review_stage: int
    interval_days: int
    tau_days: float
    depth: int
    #: 按当下时间衰减后的值(账本存的是上次记账时的值, 前端不该自己算衰减)。
    decayed_mastery: float
    next_review_at: datetime | None = None
    last_evidence_at: datetime | None = None
    updated_at: datetime | None = None


class MasteryListOut(BaseModel):
    as_of: datetime
    count: int
    items: tuple[MasteryRowOut, ...] = ()
    concepts: tuple[ConceptMasteryOut, ...] = Field(
        default=(), description="按概念聚合的加权掌握度(报告页/概念图页共用)"
    )


class ReviewItemOut(BaseModel):
    atom_key: str
    statement: str
    concept_slug: str
    due_at: datetime
    interval_days: int
    overdue_days: float = Field(ge=0.0)
    mastery: float
    review_stage: int


class ReviewQueueOut(BaseModel):
    """今日复习队列(「90 秒重讲」的入口数据)。

    只包含**记过账**且已到期的 Atom: 从没记过账的新 Atom 没有复习义务,
    它该走正常的费曼循环而不是被塞进队列(``mastery.scheduled_review`` 返回 ``None``)。
    """

    as_of: datetime
    count: int
    limit: int | None = None
    items: tuple[ReviewItemOut, ...] = ()


__all__ = [
    "ATOM_KEY_PATTERN",
    "PERM_ATOM_ADMIN",
    "PERM_EXPLANATION_WRITE",
    "PERM_REPORT_READ",
    "PERM_SELF_READ",
    "SLUG_PATTERN",
    "AtomEdgeOut",
    "AtomIn",
    "AtomMasteryOut",
    "AtomOut",
    "AtomSourceIn",
    "ConceptGraphOut",
    "ConceptMasteryOut",
    "ConceptOut",
    "ConceptUpsertRequest",
    "ConceptUpsertResult",
    "EvidenceIn",
    "ExplanationCreate",
    "ExplanationItem",
    "ExplanationListOut",
    "ExplanationOut",
    "MasteryDeltaOut",
    "MasteryListOut",
    "MasteryRowOut",
    "ReviewItemOut",
    "ReviewQueueOut",
]
