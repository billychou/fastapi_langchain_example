"""学习内核表的落库行为: 内存 SQLite 建真表, 验证约束/类型/往返。

为什么值得单测(而不是等 API 测试覆盖):

- 这五张表是**证据的地基**: 唯一约束错一位, 掌握度账本就会出现同一个人同一个 Atom
  两行, 后续所有复习调度都失去意义;
- ``MasteryLedger`` 必须能整条还原 :class:`app.pedagogy.mastery.MasteryEntry`
  (``m + α·w·(1−m)`` / ``m·exp(−Δt/τ)`` 的输入全部可回溯), 这条往返契约在这里钉死,
  ``services/learning_service.py``(P0 #6/#7)只是它的调用方;
- 级联删除是隐私口径(注销账号 = 学习证据一并清理), 不能只在 SQL 注释里承诺。

夹具约束: 与 conftest 冻结的死地址配合, 全程不碰 MySQL/Redis; SQLite 下 BigInteger
自增需要方言层改写(与 test_account_api 同一手法)。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import get_args

import pytest
import sqlalchemy as sa
from app.db.session import Base
from app.models import (
    ATOM_RELATIONS,
    Account,
    AuthCredential,
    LearnerExplanation,
    LearningAtom,
    LearningAtomEdge,
    LearningConcept,
    MasteryLedger,
    UserProfile,
)
from app.pedagogy.mastery import MasteryEntry, scheduled_review
from app.pedagogy.schemas import AtomKind
from app.pedagogy.states import FeynmanState
from sqlalchemy import BigInteger, event, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session


@compiles(BigInteger, "sqlite")
def _sqlite_bigint_as_integer(type_, compiler, **kw):  # noqa: ANN001, ARG001
    """SQLite 只把 ``INTEGER PRIMARY KEY`` 当 rowid 别名, ``BIGINT`` 主键不会自增。"""
    return "INTEGER"


LEARNING_TABLES = (
    LearningConcept.__table__,
    LearningAtom.__table__,
    LearningAtomEdge.__table__,
    LearnerExplanation.__table__,
    MasteryLedger.__table__,
)


# ``account`` 的两个关系必须一并建表: ``profile`` 是 ``lazy="selectin"``,
# 少了 ``user_profile`` 会让任何 Account 加载都炸在 "no such table"(测试的错, 不是模型的错)。
ACCOUNT_TABLES = (
    Account.__table__,
    UserProfile.__table__,
    AuthCredential.__table__,
)


@pytest.fixture()
async def session():
    """内存库 + 五张学习表(``account`` 及其从属表一起建, 让外键真的可校验)。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_sqlite_fks(dbapi_connection, _record):  # noqa: ANN001
        """SQLite 默认不强制外键; 打开才能测到 ON DELETE CASCADE。"""
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn, tables=[*ACCOUNT_TABLES, *LEARNING_TABLES]
            )
        )
        await conn.execute(insert(Account).values(id=1, login_policy="multi_device"))
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with factory() as db:
        yield db
    await engine.dispose()


async def _seed_graph(session: AsyncSession) -> tuple[LearningConcept, LearningAtom, LearningAtom]:
    """一个概念 + 两个已溯源 Atom + 一条 depends_on 边(权重 0.8)。"""
    concept = LearningConcept(
        slug="attention-scaled-dot-product",
        title="缩放点积注意力",
        subject="深度学习",
        scope="只讨论 Q/K/V 的缩放点积形式, 不涉及多头与工程实现",
    )
    session.add(concept)
    await session.flush()

    definition = LearningAtom(
        atom_key="atom:attention:score-formula",
        concept_id=concept.id,
        statement="注意力分数是 Q 与 K 的点积除以 sqrt(d_k)",
        kind="definition",
        source_title="Attention Is All You Need",
        source_locator="§3.2.1, p.4",
        verified=True,
        difficulty=2,
    )
    derivable = LearningAtom(
        atom_key="atom:attention:why-scale",
        concept_id=concept.id,
        statement="除以 sqrt(d_k) 是为了把方差拉回 1, 避免 softmax 进入饱和区",
        kind="derivable",
        verified=True,
    )
    session.add_all([definition, derivable])
    await session.flush()
    session.add(
        LearningAtomEdge(
            from_atom_id=derivable.id,
            to_atom_id=definition.id,
            relation="depends_on",
            weight=Decimal("0.800"),
        )
    )
    await session.commit()
    return concept, definition, derivable


# --------------------------------------------------------------------------- 往返


async def test_knowledge_graph_roundtrips(session: AsyncSession):
    """概念 → Atom → 依赖边: 写进去读出来必须逐字段相等(含 DECIMAL 精度)。"""
    concept, definition, derivable = await _seed_graph(session)

    loaded = await session.get(LearningConcept, concept.id)
    assert loaded is not None
    assert loaded.slug == "attention-scaled-dot-product"
    assert loaded.scope is not None and "不涉及多头" in loaded.scope

    atoms = (
        await session.scalars(select(LearningAtom).where(LearningAtom.concept_id == concept.id))
    ).all()
    assert {atom.kind for atom in atoms} == {"definition", "derivable"}
    assert all(atom.verified for atom in atoms)

    edge = (await session.scalars(select(LearningAtomEdge))).one()
    assert (edge.from_atom_id, edge.to_atom_id) == (derivable.id, definition.id)
    assert edge.relation == "depends_on"
    assert edge.weight == Decimal("0.800")
    assert isinstance(edge.weight, Decimal)  # DECIMAL 不塌成 float(账本不容浮点噪声)


async def test_explanation_evidence_roundtrips(session: AsyncSession):
    """讲解证据: 原话 + claims + gap 报告 + 说话占比, 全部原样落库。"""
    concept, _, _ = await _seed_graph(session)
    claims = [
        {"id": "c1", "text": "点积越大越相关", "status": "supported", "gap_type": None},
        {"id": "c2", "text": "除以 d_k 是为了变慢", "status": "wrong", "gap_type": "causal_gap"},
    ]
    gap_report = {
        "verdict": "not_closed",
        "gaps": [{"type": "causal_gap", "probe": "为什么是 sqrt(d_k) 而不是 d_k?", "severity": 2}],
        "scores": {"completeness": 0.4, "own_words": 0.8, "boundary_awareness": 0.0},
    }
    session.add(
        LearnerExplanation(
            account_id=1,
            thread_id="0d6f0f2a-2a5f-4d6b-9d1e-2f6a1d3c9b10",
            concept_id=concept.id,
            loop_index=2,
            state=FeynmanState.DETECT,
            raw_text="注意力就是把 Q 和 K 点积起来, 再除以 d_k 让它小一点……",
            claims_json=claims,
            gap_report_json=gap_report,
            learner_talk_ratio=Decimal("0.618"),
        )
    )
    await session.commit()

    row = (await session.scalars(select(LearnerExplanation))).one()
    assert row.state == "Detect"  # StrEnum 落库为字符串
    assert row.loop_index == 2
    assert row.raw_text.startswith("注意力就是把 Q 和 K 点积起来")
    assert row.claims_json == claims
    assert row.gap_report_json == gap_report
    assert row.learner_talk_ratio == Decimal("0.618")
    assert row.created_at is not None


async def test_ledger_roundtrips_a_mastery_entry(session: AsyncSession):
    """账本一行必须能整条还原 ``MasteryEntry``(掌握度公式的输入全部可回溯)。

    映射契约: ``mastery/review_stage(=ladder_index)/tau_days/evidence_count/depth/
    updated_at`` 落列; ``weight`` 取依赖边, ``concept_id`` 由 Atom 反查;
    ``atom_id`` 在库里是 BIGINT, 在内核里是 ``atom_key`` 字符串。
    service 层(P0 #6/#7)是这套映射的唯一实现方, 这里钉住它的输入输出。
    """
    concept, definition, _ = await _seed_graph(session)
    stamp = datetime(2026, 9, 14, 3, 0, 0, tzinfo=UTC)
    entry = MasteryEntry(
        concept_id=concept.slug,
        atom_id=definition.atom_key,
        mastery=0.675,
        weight=0.8,
        ladder_index=2,
        tau_days=7.0,
        evidence_count=1,
        depth=3,
        updated_at=stamp,
    )

    session.add(
        MasteryLedger(
            account_id=1,
            atom_id=definition.id,
            mastery=Decimal(str(entry.mastery)),
            evidence_count=entry.evidence_count,
            last_evidence_at=entry.updated_at,
            next_review_at=scheduled_review(entry),  # updated_at + 阶梯间隔(7 天)
            review_stage=entry.ladder_index,
            tau_days=Decimal(str(entry.tau_days)),
            depth=entry.depth,
            updated_at=stamp,
        )
    )
    await session.commit()

    row = (await session.scalars(select(MasteryLedger))).one()
    atom = await session.get(LearningAtom, row.atom_id)
    assert atom is not None
    edge = (await session.scalars(select(LearningAtomEdge))).one()
    restored = MasteryEntry(
        concept_id=(await session.get(LearningConcept, atom.concept_id)).slug,
        atom_id=atom.atom_key,
        mastery=float(row.mastery),
        weight=float(edge.weight),
        ladder_index=row.review_stage,
        tau_days=float(row.tau_days),
        evidence_count=row.evidence_count,
        depth=row.depth,
        updated_at=row.updated_at.replace(tzinfo=UTC),
    )
    assert restored.mastery == pytest.approx(entry.mastery)
    assert restored.weight == pytest.approx(entry.weight)
    assert restored.tau_days == pytest.approx(entry.tau_days)
    assert restored == entry.model_copy(update={"updated_at": restored.updated_at})
    assert row.mastery == Decimal("0.6750")  # DECIMAL(5,4): 文档公式的示例值原样保存
    assert row.next_review_at == stamp.replace(tzinfo=None) + timedelta(days=7)


# --------------------------------------------------------------------------- 约束


async def test_ledger_is_unique_per_account_and_atom(session: AsyncSession):
    """``uk_mastery``: 同一个人同一个 Atom 只能有一行, 否则复习调度失去意义。"""
    _, definition, _ = await _seed_graph(session)
    session.add(MasteryLedger(account_id=1, atom_id=definition.id))
    await session.commit()
    session.add(MasteryLedger(account_id=1, atom_id=definition.id))
    with pytest.raises(sa.exc.IntegrityError):
        await session.commit()
    await session.rollback()


async def test_same_atom_can_have_one_row_per_account(session: AsyncSession):
    """唯一约束是 (account_id, atom_id) 而不是 atom_id: 学习者之间互不干扰。"""
    _, definition, _ = await _seed_graph(session)
    session.add(Account(id=2, login_policy="multi_device"))
    session.add(MasteryLedger(account_id=1, atom_id=definition.id))
    session.add(MasteryLedger(account_id=2, atom_id=definition.id))
    await session.commit()
    rows = (await session.scalars(select(MasteryLedger))).all()
    assert {row.account_id for row in rows} == {1, 2}


async def test_atom_edge_unique_per_relation(session: AsyncSession):
    """``uk_edge``: 同一对 Atom 同一种关系只有一条边; 换一种关系是另一条边。"""
    _, definition, derivable = await _seed_graph(session)
    # rollback 会把 identity map 里所有实例标记为过期, 之后访问 ``.id`` 会在 async
    # 会话里触发同步刷新; 所以先把主键固化成 int 再制造冲突。
    head_id, tail_id = derivable.id, definition.id
    session.add(
        LearningAtomEdge(from_atom_id=head_id, to_atom_id=tail_id, relation="depends_on")
    )
    with pytest.raises(sa.exc.IntegrityError):
        await session.commit()
    await session.rollback()

    session.add(LearningAtomEdge(from_atom_id=head_id, to_atom_id=tail_id, relation="entails"))
    await session.commit()
    assert len((await session.scalars(select(LearningAtomEdge))).all()) == 2


async def test_atom_key_and_concept_slug_are_unique(session: AsyncSession):
    """稳定字符串 ID 是内核的唯一入口, 重复就等于两个概念共用一个身份。"""
    concept, definition, _ = await _seed_graph(session)
    slug, concept_id, atom_key = concept.slug, concept.id, definition.atom_key

    session.add(LearningConcept(slug=slug, title="重复的概念"))
    with pytest.raises(sa.exc.IntegrityError):
        await session.commit()
    await session.rollback()

    session.add(
        LearningAtom(
            atom_key=atom_key,
            concept_id=concept_id,
            statement="重复的 Atom",
            kind="definition",
        )
    )
    with pytest.raises(sa.exc.IntegrityError):
        await session.commit()
    await session.rollback()

    # 冲突回滚之后, 图数据必须完好(唯一约束只挡重复, 不破坏已有证据)。
    assert len((await session.scalars(select(LearningAtom))).all()) == 2


async def test_server_defaults_fill_unverified_atom_and_empty_ledger(session: AsyncSession):
    """服务端默认值 = 安全默认: 未溯源(verified=0)、掌握度 0、阶梯 0、τ 取冷启动 7 天。

    用 Core INSERT 刻意不给这些列, 验证默认值来自**数据库**而不是 ORM 的 Python 默认,
    这样手写 SQL / 数据脚本 / 迁移三条路径的行为一致。
    """
    concept = LearningConcept(slug="softmax", title="Softmax")
    session.add(concept)
    await session.flush()

    await session.execute(
        insert(LearningAtom.__table__).values(
            atom_key="atom:softmax:definition",
            concept_id=concept.id,
            statement="softmax 把向量归一化为概率分布",
            kind="definition",
        )
    )
    await session.execute(insert(MasteryLedger.__table__).values(account_id=1, atom_id=1))
    await session.commit()

    atom = (await session.scalars(select(LearningAtom))).one()
    assert atom.verified is False  # 未溯源 -> 不得进入掌握度记账
    assert atom.difficulty == 1  # 与 schemas.Atom 的默认难度一致

    ledger = (await session.scalars(select(MasteryLedger))).one()
    assert ledger.mastery == Decimal("0.0000")
    assert ledger.evidence_count == 0
    assert ledger.review_stage == 0
    assert ledger.depth == 0
    assert ledger.tau_days == Decimal("7.000")
    assert ledger.next_review_at is None  # 还没记过账 -> 不进复习队列
    assert ledger.updated_at is not None


async def test_account_deletion_cascades_to_learning_evidence(session: AsyncSession):
    """注销账号必须带走学习证据与账本(隐私口径), 概念图作为公共资产保留。"""
    from app.models import Account

    concept, definition, _ = await _seed_graph(session)
    session.add(
        LearnerExplanation(
            account_id=1,
            concept_id=concept.id,
            state=FeynmanState.TEACH,
            raw_text="我觉得它就是加权平均",
            claims_json=[],
        )
    )
    session.add(MasteryLedger(account_id=1, atom_id=definition.id))
    await session.commit()

    account = await session.get(Account, 1)
    assert account is not None
    await session.delete(account)
    await session.commit()

    assert (await session.scalars(select(LearnerExplanation))).all() == []
    assert (await session.scalars(select(MasteryLedger))).all() == []
    assert (await session.scalars(select(LearningAtom))).all() != []


async def test_concept_deletion_cascades_to_atoms_and_edges(session: AsyncSession):
    """概念下线时 Atom 与依赖边一起清理: 不留悬挂的图节点。"""
    concept, definition, _ = await _seed_graph(session)
    session.add(MasteryLedger(account_id=1, atom_id=definition.id))
    await session.commit()

    loaded = await session.get(LearningConcept, concept.id)
    assert loaded is not None
    await session.delete(loaded)
    await session.commit()

    assert (await session.scalars(select(LearningAtom))).all() == []
    assert (await session.scalars(select(LearningAtomEdge))).all() == []
    assert (await session.scalars(select(MasteryLedger))).all() == []


# --------------------------------------------------------------------------- 词表与列宽


def test_state_column_fits_every_feynman_state():
    """``state`` 列宽必须容得下七态里最长的那个, 否则闭合时会静默截断。"""
    width = LearnerExplanation.__table__.columns["state"].type.length
    longest = max(len(state.value) for state in FeynmanState)
    assert width is not None and width >= longest
    assert longest == len("Simplify")


def test_atom_kind_column_matches_pedagogy_vocabulary():
    """``kind`` 列宽与词表来自内核(``AtomKind``), 数据库层不重复定义真值。"""
    kinds = get_args(AtomKind)
    assert set(kinds) == {"definition", "axiom", "observable", "derivable"}
    width = LearningAtom.__table__.columns["kind"].type.length
    assert width is not None and width >= max(len(kind) for kind in kinds)
    comment = LearningAtom.__table__.columns["kind"].comment or ""
    for kind in kinds:
        assert kind in comment, f"{kind} 未写进 kind 列的注释(教研看 DDL 时认不出词表)"


def test_atom_relations_match_ddl_comment():
    """依赖边词表(含留给误解库的 ``contradicts``)必须与 DDL 注释一致。"""
    assert ATOM_RELATIONS == ("depends_on", "entails", "contradicts")
    comment = LearningAtomEdge.__table__.columns["relation"].comment or ""
    assert comment == "|".join(ATOM_RELATIONS)


def test_numeric_columns_use_documented_precision():
    """掌握度 DECIMAL(5,4)、边权重 DECIMAL(4,3)、说话占比 DECIMAL(4,3)(文档口径)。"""
    def _scale(table: sa.Table, name: str) -> tuple[int, int]:
        column = table.columns[name].type
        return (column.precision, column.scale)

    assert _scale(MasteryLedger.__table__, "mastery") == (5, 4)
    assert _scale(MasteryLedger.__table__, "tau_days") == (6, 3)
    assert _scale(LearningAtomEdge.__table__, "weight") == (4, 3)
    assert _scale(LearnerExplanation.__table__, "learner_talk_ratio") == (4, 3)


def test_raw_text_uses_mediumtext_on_mysql():
    """学习者原话可能很长: MySQL 必须走 MEDIUMTEXT, 其他方言退回 TEXT。"""
    from sqlalchemy.dialects import mysql

    column_type = LearnerExplanation.__table__.columns["raw_text"].type
    assert str(column_type.compile(dialect=mysql.dialect())) == "MEDIUMTEXT"
    generic = sa.create_engine("sqlite://").dialect
    assert str(column_type.compile(dialect=generic)) == "TEXT"


def test_review_queue_index_exists():
    """「今日复习队列」是账本唯一的热点查询, 必须有 (account_id, next_review_at) 索引。"""
    indexes = {index.name: tuple(column.name for column in index.columns) for index in MasteryLedger.__table__.indexes}
    assert indexes["idx_mastery_review"] == ("account_id", "next_review_at")


def test_explanation_lookup_index_exists():
    """按 (账号, 概念, 时间) 查历史讲解是报告页的主查询路径。"""
    indexes = {
        index.name: tuple(column.name for column in index.columns)
        for index in LearnerExplanation.__table__.indexes
    }
    assert indexes["idx_expl_account"] == ("account_id", "concept_id", "created_at")


def test_models_are_usable_from_a_sync_session():
    """模型不绑定异步 API: 教研脚本/数据导入可以用同步 Session 直接写。"""
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=list(LEARNING_TABLES))
    with Session(engine) as db:
        concept = LearningConcept(slug="backprop", title="反向传播")
        db.add(concept)
        db.commit()
        assert db.scalar(select(LearningConcept).where(LearningConcept.slug == "backprop")) is not None
    engine.dispose()
