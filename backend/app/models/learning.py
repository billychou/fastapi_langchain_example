"""学习证据与知识图的持久化模型(见 ``docs/education-agent-architecture.md`` §4)。

五张表分两组:

**知识图**(学科无关、全局共享、与账号无关)::

    learning_concept   概念节点(``slug`` 即内核里的 ``concept_id`` 字符串)
    learning_atom      知识原子(``atom_key`` 即 ``Atom.id``; ``verified=false`` 不得记账)
    learning_atom_edge 原子依赖边(depends_on/entails/contradicts —— 第一性原理的图化身)

**学习证据**(挂在账号上, 可审计、可回放)::

    learner_explanation 一次自述的原话 + claims + gap 报告(费曼循环的原始产物)
    mastery_ledger      每个 (账号, Atom) 一行的掌握度账本 + 间隔重复调度

设计约束(为什么长这样):

1. **两套 ID, 一次翻译**: 内核(``app/pedagogy``)只认稳定字符串
   (``Atom.id`` ↔ ``learning_atom.atom_key``, ``concept_id`` ↔ ``learning_concept.slug``),
   数据库用自增 BIGINT 主键 + 唯一字符串键。翻译只发生在
   ``services/learning_service.py``, 内核不认识数据库主键, 因此换库/重排 ID 不影响判分。
2. **账目可回溯**: :class:`MasteryLedger` 与
   :class:`app.pedagogy.mastery.MasteryEntry` 一一对应 ——
   ``mastery / review_stage(=ladder_index) / tau_days / evidence_count / depth / updated_at``
   直接落列, ``weight`` 取 ``learning_atom_edge.weight``, ``concept_id`` 由
   ``learning_atom.concept_id`` 反查。掌握度公式(``m + α·w·(1−m)``、``m·exp(−Δt/τ)``)
   的每一个输入都能从一行账目还原, 这是「记账只有一个入口」能自证的前提。
3. **数值列用 DECIMAL 不用 FLOAT**: 掌握度是要被家长/教研审视的证据, 不能出现
   ``0.6749999`` 这种浮点噪声; ``Decimal ↔ float`` 的换算同样只在 service 层发生。
4. **默认值放服务端**: ``server_default`` 与 ``migrations/005_learning.sql`` 的
   ``DEFAULT`` 逐字一致, 所以「迁移建表」「initdb 建表」「手写 SQL 插入」三条路径
   得到同一张表 —— 一致性由 ``tests/test_migrations.py`` 守卫。
5. **原话不加工**: ``learner_explanation.raw_text`` 存学习者原话(MySQL 用 MEDIUMTEXT),
   拆解与判分结果分别放 ``claims_json`` / ``gap_report_json`` —— 判分器改了 rubric
   也能拿原话重跑, 证据不会被结论污染。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base

#: 依赖边关系词表(与 DDL comment 逐字一致)。
#: ``contradicts`` 留给 P1 的误解库: 学习者的错误命题与正确 Atom 之间的冲突边。
ATOM_RELATIONS: tuple[str, ...] = ("depends_on", "entails", "contradicts")

#: 讲解证据里 ``state`` 列的取值来自 :class:`app.pedagogy.states.FeynmanState`;
#: 这里只声明列宽, 词表本身不在数据库层重复定义(避免两处真值)。
STATE_COLUMN_WIDTH = 16


def _mediumtext() -> Text:
    """学习者原话: MySQL 用 MEDIUMTEXT(自述可能很长), 其他方言退回 TEXT。"""
    return Text().with_variant(MEDIUMTEXT(), "mysql")


class LearningConcept(Base):
    """概念节点: 一轮费曼循环的对象(``slug`` 是内核查概念的唯一入口)。"""

    __tablename__ = "learning_concept"
    __table_args__ = (UniqueConstraint("slug", name="uk_concept_slug"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(
        String(128), comment="概念标识, 如 attention-scaled-dot-product"
    )
    title: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="学科标签, 仅用于筛选, 不参与内核逻辑"
    )
    scope: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="范围边界: 明确不讨论什么(no_boundary 卡点的判据)"
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class LearningAtom(Base):
    """知识原子: 一条可判真伪的最小命题(Why-chain 的下探落点)。"""

    __tablename__ = "learning_atom"
    __table_args__ = (
        UniqueConstraint("atom_key", name="uk_atom_key"),
        Index("idx_atom_concept", "concept_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    atom_key: Mapped[str] = mapped_column(
        String(191), comment="稳定标识 atom:xxx(即内核 Atom.id)"
    )
    concept_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("learning_concept.id", ondelete="CASCADE"), comment="所属概念"
    )
    statement: Mapped[str] = mapped_column(Text, comment="不可再分的最小命题")
    kind: Mapped[str] = mapped_column(
        String(16), comment="definition|axiom|observable|derivable(前三者是 Why-chain 可停点)"
    )
    source_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_locator: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="章节/页码/时间戳"
    )
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    verified: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=text("0"),
        comment="未溯源不得进入掌握度记账",
    )
    difficulty: Mapped[int] = mapped_column(
        SmallInteger,
        default=1,
        server_default=text("1"),
        comment="1-5, 出题难度校准用(默认值与 schemas.Atom 一致)",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class LearningAtomEdge(Base):
    """原子依赖边: ``from_atom_id --relation--> to_atom_id``。

    ``weight`` 决定概念掌握度 ``M = Σ m·weight / Σ weight`` 的分母 —— 关键边
    (被更多 Atom 依赖)权重更高, 所以它属于图而不是属于账本。
    """

    __tablename__ = "learning_atom_edge"
    __table_args__ = (
        UniqueConstraint("from_atom_id", "to_atom_id", "relation", name="uk_edge"),
        Index("idx_edge_from", "from_atom_id"),
        Index("idx_edge_to", "to_atom_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    from_atom_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("learning_atom.id", ondelete="CASCADE")
    )
    to_atom_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("learning_atom.id", ondelete="CASCADE")
    )
    relation: Mapped[str] = mapped_column(String(16), comment="depends_on|entails|contradicts")
    weight: Mapped[Decimal] = mapped_column(
        Numeric(4, 3),
        default=Decimal("1.000"),
        server_default=text("1.000"),
        comment="关键边权重更高",
    )


class LearnerExplanation(Base):
    """讲解证据: 费曼循环每一轮 ``Teach`` 的原始产物。

    一行 = 学习者的一次自述 + 内核对它的拆解与诊断。判分器换 rubric 时可以拿
    ``raw_text`` 重跑, 所以原话与结论分列存储, 不互相覆盖。
    """

    __tablename__ = "learner_explanation"
    __table_args__ = (Index("idx_expl_account", "account_id", "concept_id", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("account.id", ondelete="CASCADE"), comment="学习者账号"
    )
    thread_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="关联 LangGraph thread(L1 记忆)"
    )
    concept_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("learning_concept.id", ondelete="CASCADE")
    )
    loop_index: Mapped[int] = mapped_column(
        SmallInteger,
        default=1,
        server_default=text("1"),
        comment="第几轮 Teach(1 起, 上限 PedagogyConfig.max_loops)",
    )
    state: Mapped[str] = mapped_column(
        String(STATE_COLUMN_WIDTH), comment="Pick|Teach|Detect|Return|Simplify|Closed|Escaped"
    )
    raw_text: Mapped[str] = mapped_column(_mediumtext(), comment="学习者原话, 不加工")
    claims_json: Mapped[list] = mapped_column(JSON, comment="命题拆解 + status")
    gap_report_json: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, comment="四类卡点 + probe + supply"
    )
    learner_talk_ratio: Mapped[Decimal | None] = mapped_column(
        Numeric(4, 3), nullable=True, comment="learner_chars/(learner+system), 护栏口径"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class MasteryLedger(Base):
    """掌握度账本: 每个 (账号, Atom) 一行, 每个数字都追得到证据。

    数值只由 :func:`app.pedagogy.mastery.record` 经由 service 层写入;
    ``next_review_at`` 是 :func:`app.pedagogy.mastery.scheduled_review` 的落库结果,
    建 ``(account_id, next_review_at)`` 索引只为「今日复习队列」这一条查询服务。
    """

    __tablename__ = "mastery_ledger"
    __table_args__ = (
        UniqueConstraint("account_id", "atom_id", name="uk_mastery"),
        Index("idx_mastery_review", "account_id", "next_review_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("account.id", ondelete="CASCADE")
    )
    atom_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("learning_atom.id", ondelete="CASCADE")
    )
    mastery: Mapped[Decimal] = mapped_column(
        Numeric(5, 4),
        default=Decimal("0.0000"),
        server_default=text("0.0000"),
        comment="掌握度 m ∈ [0,1]",
    )
    evidence_count: Mapped[int] = mapped_column(
        Integer,
        default=0,
        server_default=text("0"),
        comment="有效证据条数(读直答/放弃不计数)",
    )
    last_evidence_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_review_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="间隔重复调度(1/3/7/21/60 天阶梯)"
    )
    review_stage: Mapped[int] = mapped_column(
        SmallInteger,
        default=0,
        server_default=text("0"),
        comment="复习阶梯下标(REVIEW_LADDER_DAYS 的索引)",
    )
    tau_days: Mapped[Decimal] = mapped_column(
        Numeric(6, 3),
        default=Decimal("7.000"),
        server_default=text("7.000"),
        comment="遗忘时间常数 τ(天): 冷启动取配置, 之后按该 Atom 历史表现拟合",
    )
    depth: Mapped[int] = mapped_column(
        SmallInteger,
        default=0,
        server_default=text("0"),
        comment="最近一次 Why-chain 下探深度(证据权重修正用)",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), server_onupdate=func.now()
    )
