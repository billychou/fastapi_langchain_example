"""学习证据的业务编排: 概念图 / 讲解证据 / 掌握度账本 / 复习队列。

这一层存在的唯一理由是**翻译**(见 ``app/models/learning.py`` 的设计约束 1):

    内核(app/pedagogy)     <->  service  <->     数据库
    concept_id: str                     learning_concept.slug / .id: int
    Atom.id: str                        learning_atom.atom_key / .id: int
    MasteryEntry(mastery: float)        mastery_ledger.mastery: Decimal

内核不认识数据库主键, 路由不认识 ORM 行 —— 于是「换库/重排 ID」不影响判分,
「DECIMAL 不出现浮点噪声」也不影响内核的纯函数。所有换算集中在本文件。

三条硬约束:

1. **掌握度数值的写入口只有 :func:`app.pedagogy.mastery.record`**(经
   :func:`record_evidence`)。本文件不自己算 ``m + α·w·(1−m)``, 也不自己算衰减 ——
   账本上每个数字都必须能由一条证据 + 一个公式还原, 否则「证据优先」就是口号。
2. **不记账就不写库。** ``record`` 返回 ``credited=False`` 时(读直答/放弃/Atom 未溯源)
   数值原样保留, 且**不移动 ``updated_at``** —— 移动它等于把复习时钟往后推:
   学习者读了一次直答, 本该今天复习的概念就延期了。
3. **属主校验在 service, 不在路由。** 读他人数据一律返回 ``None``(路由转 404),
   与 ``thread_service.get_thread`` / skills 的防枚举口径一致: 不存在与不属于你
   在响应上不可区分。
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import BizCode, BizError
from app.models.learning import (
    LearnerExplanation,
    LearningAtom,
    LearningAtomEdge,
    LearningConcept,
    MasteryLedger,
)
from app.pedagogy.mastery import (
    MasteryEntry,
    MasteryUpdate,
    concept_mastery,
    decay,
    elapsed_days_since,
    interval_days,
    new_entry,
    record_many,
    scheduled_review,
)
from app.pedagogy.mastery import (
    review_queue as compute_review_queue,
)
from app.pedagogy.policy import PedagogyConfig
from app.pedagogy.turn import concept_slug
from app.schemas.learning import (
    AtomEdgeOut,
    AtomIn,
    AtomMasteryOut,
    AtomOut,
    ConceptGraphOut,
    ConceptMasteryOut,
    ConceptOut,
    ConceptUpsertRequest,
    ConceptUpsertResult,
    ExplanationCreate,
    ExplanationItem,
    ExplanationListOut,
    ExplanationOut,
    MasteryDeltaOut,
    MasteryListOut,
    MasteryRowOut,
    ReviewItemOut,
    ReviewQueueOut,
)

logger = logging.getLogger("learning")

#: 列表接口默认条数(v1 不分页, 与 ``thread_service.LIST_LIMIT`` 同一量级)。
EXPLANATION_LIST_LIMIT = 50
EXPLANATION_LIST_MAX = 200
#: 复习队列的默认上限: 前端是「今天先复习 N 个」的口径, 一次吐 400 个 Atom 没有意义。
REVIEW_QUEUE_LIMIT = 50
REVIEW_QUEUE_MAX = 200
#: 一次记账最多涉及多少个 Atom(防止一条请求改写整张账本)。
EVIDENCE_ATOM_MAX = 64


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _decimal(value: float | int | str | Decimal | None, default: str = "0") -> Decimal:
    """``float -> Decimal`` 的唯一入口: 走字符串, 避开 ``Decimal(0.1)`` 的二进制噪声。"""
    if value is None:
        return Decimal(default)
    return Decimal(str(value))


def _float(value: Decimal | float | None, default: float = 0.0) -> float:
    return default if value is None else float(value)


# --------------------------------------------------------------------------- 概念与知识图


async def get_concept(db: AsyncSession, slug: str) -> LearningConcept | None:
    """按 slug 取概念(``slug`` 是内核查概念的唯一入口)。"""
    statement = select(LearningConcept).where(LearningConcept.slug == slug)
    return (await db.execute(statement)).scalar_one_or_none()


def concept_out(row: LearningConcept) -> ConceptOut:
    return ConceptOut(
        id=row.id,
        slug=row.slug,
        title=row.title,
        subject=row.subject,
        scope=row.scope,
        summary=row.summary,
        created_at=row.created_at,
    )


def atom_out(row: LearningAtom) -> AtomOut:
    return AtomOut(
        atom_key=row.atom_key,
        statement=row.statement,
        kind=row.kind,
        verified=bool(row.verified),
        difficulty=int(row.difficulty),
        source_title=row.source_title,
        source_locator=row.source_locator,
        source_url=row.source_url,
    )


async def bind_concept(
    db: AsyncSession, payload: ConceptUpsertRequest
) -> ConceptUpsertResult:
    """建立或绑定一个概念, 并 upsert 它的前置 Atom 子图。

    - ``slug`` 缺省时由 ``title`` 经 :func:`app.pedagogy.turn.concept_slug` 生成:
      必须与聊天里「我想搞懂闭包」得到的是同一个 slug, 否则同一个概念会有两行,
      掌握度账本当场分叉;
    - 已存在时**只用调用者显式给出的字段覆盖**(``model_fields_set``), 不传就不动 ——
      一次绑定不该把教研写好的 ``scope`` 抹成 ``None``;
    - ``replace_atoms=true`` 是破坏性操作: 被删掉的 Atom 连带删掉它的全部账本行
      (证据挂在 Atom 上, Atom 没了证据无处可挂), 所以路由把它门禁在
      ``learning:atom:admin``。
    """
    slug = payload.slug or concept_slug(payload.title)
    row = await get_concept(db, slug)
    created = row is None
    if row is None:
        row = LearningConcept(slug=slug, title=payload.title)
        db.add(row)
    else:
        provided = payload.model_fields_set
        if "title" in provided:
            row.title = payload.title
        for field in ("subject", "scope", "summary"):
            if field in provided:
                setattr(row, field, getattr(payload, field))
    await db.flush()  # 拿到 row.id(后续 Atom/边都要它)

    existing = await _atoms_by_concept(db, row.id)
    created_keys: list[str] = []
    updated_keys: list[str] = []
    for atom in payload.atoms:
        target = existing.get(atom.atom_key)
        if target is None:
            db.add(_new_atom(row.id, atom))
            created_keys.append(atom.atom_key)
        else:
            _update_atom(target, atom)
            updated_keys.append(atom.atom_key)

    removed_keys: list[str] = []
    if payload.replace_atoms:
        keep = {atom.atom_key for atom in payload.atoms}
        stale = [item for key, item in existing.items() if key not in keep]
        if stale:
            removed_keys = [item.atom_key for item in stale]
            # 账本行先删: mastery_ledger.atom_id 是外键, 留着会违反约束
            await db.execute(
                delete(MasteryLedger).where(
                    MasteryLedger.atom_id.in_([item.id for item in stale])
                )
            )
            await db.execute(
                delete(LearningAtomEdge).where(
                    LearningAtomEdge.from_atom_id.in_([item.id for item in stale])
                    | LearningAtomEdge.to_atom_id.in_([item.id for item in stale])
                )
            )
            for item in stale:
                await db.delete(item)

    await db.flush()
    atoms = await _atoms_by_concept(db, row.id)
    unknown = _unknown_edge_targets(payload.atoms, atoms)
    if unknown:
        # 不静默跳过: 判分器/教研脚本给错一个 key, 图上就会少一条边, 而没有任何报错。
        raise BizError(BizCode.BAD_REQUEST, f"依赖边指向不存在的 Atom: {sorted(unknown)}")
    await _sync_edges(db, atoms, payload.atoms)
    await db.commit()

    atoms = await _atoms_by_concept(db, row.id)
    edges = await _edges_of(db, list(atoms.values()))
    concept = await get_concept(db, slug)
    assert concept is not None  # 刚 flush/commit 过
    return ConceptUpsertResult(
        concept=concept_out(concept),
        created=created,
        atoms=tuple(atom_out(item) for item in atoms.values()),
        edges=tuple(edges.values()),
        atom_keys_created=tuple(created_keys),
        atom_keys_updated=tuple(updated_keys),
        atom_keys_removed=tuple(removed_keys),
    )


def _new_atom(concept_id: int, atom: AtomIn) -> LearningAtom:
    return LearningAtom(
        atom_key=atom.atom_key,
        concept_id=concept_id,
        statement=atom.statement,
        kind=str(atom.kind),
        source_title=atom.source.title if atom.source else None,
        source_locator=atom.source.locator if atom.source else None,
        source_url=atom.source.url if atom.source else None,
        verified=atom.verified,
        difficulty=atom.difficulty,
    )


def _update_atom(row: LearningAtom, atom: AtomIn) -> None:
    """整行覆盖(除 ``atom_key``): Atom 是「一条命题」, 改了 statement 就是另一条命题,
    但键必须稳定 —— 账本靠它认人。"""
    row.statement = atom.statement
    row.kind = str(atom.kind)
    row.source_title = atom.source.title if atom.source else None
    row.source_locator = atom.source.locator if atom.source else None
    row.source_url = atom.source.url if atom.source else None
    row.verified = atom.verified
    row.difficulty = atom.difficulty


async def _atoms_by_concept(db: AsyncSession, concept_id: int) -> dict[str, LearningAtom]:
    statement = (
        select(LearningAtom).where(LearningAtom.concept_id == concept_id).order_by(LearningAtom.id)
    )
    return {row.atom_key: row for row in (await db.execute(statement)).scalars().all()}


def _unknown_edge_targets(payload_atoms: tuple[AtomIn, ...], atoms: dict[str, LearningAtom]) -> set[str]:
    known = set(atoms)
    unknown: set[str] = set()
    for atom in payload_atoms:
        unknown.update(key for key in (*atom.depends_on, *atom.entails) if key not in known)
    return unknown


async def _sync_edges(
    db: AsyncSession, atoms: dict[str, LearningAtom], payload_atoms: tuple[AtomIn, ...]
) -> None:
    """按 payload 重建这批 Atom 的出边(幂等: 已存在的边不重复插)。

    只重建 payload 里出现过的 Atom 的出边, 不动别人的 —— 增量 upsert 时,
    一次只提交两个 Atom 不该把整张图的边清掉。
    """
    wanted: dict[tuple[int, int, str], LearningAtomEdge] = {}
    for atom in payload_atoms:
        source = atoms[atom.atom_key]
        for relation, keys in (("depends_on", atom.depends_on), ("entails", atom.entails)):
            for key in keys:
                target = atoms[key]
                wanted[(source.id, target.id, relation)] = LearningAtomEdge(
                    from_atom_id=source.id, to_atom_id=target.id, relation=relation
                )
    if not wanted:
        return
    statement = select(LearningAtomEdge).where(
        LearningAtomEdge.from_atom_id.in_({key[0] for key in wanted})
    )
    existing = {
        (edge.from_atom_id, edge.to_atom_id, edge.relation)
        for edge in (await db.execute(statement)).scalars().all()
    }
    for key, edge in wanted.items():
        if key not in existing:
            db.add(edge)
    await db.flush()


async def _edges_of(db: AsyncSession, atoms: list[LearningAtom]) -> dict[tuple[int, int, str], AtomEdgeOut]:
    if not atoms:
        return {}
    ids = [atom.id for atom in atoms]
    statement = select(LearningAtomEdge).where(
        LearningAtomEdge.from_atom_id.in_(ids), LearningAtomEdge.to_atom_id.in_(ids)
    )
    keys = {atom.id: atom.atom_key for atom in atoms}
    return {
        (edge.from_atom_id, edge.to_atom_id, edge.relation): AtomEdgeOut(
            from_atom=keys[edge.from_atom_id],
            to_atom=keys[edge.to_atom_id],
            relation=edge.relation,  # type: ignore[arg-type]
            weight=_float(edge.weight, 1.0),
        )
        for edge in (await db.execute(statement)).scalars().all()
    }


async def atom_weights(db: AsyncSession, atoms: list[LearningAtom]) -> dict[int, float]:
    """Atom 的概念内权重 = 指向它的依赖边里最大的 ``weight``(默认 1.0)。

    权重属于**图**而不是账本(``learning_atom_edge.weight``): 被更多 Atom 依赖的前置
    概念更关键, 因此在 ``M = Σ m·w / Σ w`` 里占更大分母。P0 的边权重恒为建表默认值
    1.000, 所以概念掌握度退化成算术平均 —— 真正区分权重是 P1 #6(材料摄取)的事,
    这里只保证「取权重的规则」已经唯一且可测。
    """
    weights = {atom.id: 1.0 for atom in atoms}
    if not atoms:
        return weights
    ids = [atom.id for atom in atoms]
    statement = select(LearningAtomEdge).where(LearningAtomEdge.to_atom_id.in_(ids))
    for edge in (await db.execute(statement)).scalars().all():
        weight = _float(edge.weight, 1.0)
        weights[edge.to_atom_id] = max(weights.get(edge.to_atom_id, 1.0), weight)
    return weights


async def concept_graph(
    db: AsyncSession, slug: str, account_id: int, *, now: datetime | None = None
) -> ConceptGraphOut | None:
    """概念子图 + **我的**掌握度。概念不存在返回 ``None``(路由转 404)。

    他人数据不可能出现在这里: 账本查询以 ``account_id`` 为条件, 而 ``account_id``
    来自 Token, 不来自请求体。跨账号视图是 ``/learning/report`` 的事(P1 #10,
    门禁 ``learning:report:read``)。
    """
    concept = await get_concept(db, slug)
    if concept is None:
        return None
    stamp = now or _utcnow()
    atoms = await _atoms_by_concept(db, concept.id)
    rows = list(atoms.values())
    edges = await _edges_of(db, rows)
    weights = await atom_weights(db, rows)
    ledger = await _ledger_rows(db, account_id, [atom.id for atom in rows])

    mastery_out: list[AtomMasteryOut] = []
    entries: list[MasteryEntry] = []
    for atom in rows:
        weight = weights.get(atom.id, 1.0)
        row = ledger.get(atom.id)
        entry = (
            _to_entry(row, atom_key=atom.atom_key, concept_slug=concept.slug, weight=weight)
            if row is not None
            else new_entry(atom.atom_key, concept_id=concept.slug, weight=weight)
        )
        # 聚合用「衰减到当下」的值: 遗忘是设计的一部分, 概念掌握度不该停在上周的快照。
        entries.append(
            entry.model_copy(
                update={
                    "mastery": decay(
                        entry.mastery, elapsed_days_since(entry.updated_at, stamp), entry.tau_days
                    )
                }
            )
        )
        mastery_out.append(
            AtomMasteryOut(
                atom_key=atom.atom_key,
                mastery=round(_decayed(entry, stamp), 4),
                weight=weight,
                evidence_count=entry.evidence_count,
                review_stage=entry.ladder_index,
                interval_days=interval_days(entry.ladder_index),
                next_review_at=scheduled_review(entry),
                recorded=row is not None,
            )
        )
    return ConceptGraphOut(
        concept=concept_out(concept),
        atoms=tuple(atom_out(atom) for atom in rows),
        edges=tuple(edges.values()),
        mastery=tuple(mastery_out),
        concept_mastery=round(concept_mastery(entries), 4),
    )


def _decayed(entry: MasteryEntry, now: datetime) -> float:
    return decay(entry.mastery, elapsed_days_since(entry.updated_at, now), entry.tau_days)


# --------------------------------------------------------------------------- 讲解证据


async def create_explanation(
    db: AsyncSession, account_id: int, payload: ExplanationCreate, *, now: datetime | None = None
) -> ExplanationOut:
    """写入一份自述(原话 + 拆解 + 诊断), 并按需记一条掌握度证据。

    原话与结论**分列存储**: 判分器换 rubric 时拿 ``raw_text`` 重跑, 证据不会被结论污染
    (``app/models/learning.py`` 设计约束 5)。概念不存在返回 404 而**不自动建概念** ——
    建概念是知识图授权动作(``learning:atom:admin``), 学习者提交自述不该顺带造图。
    """
    concept = await get_concept(db, payload.concept_slug)
    if concept is None:
        raise BizError(BizCode.NOT_FOUND, "概念不存在")

    stamp = now or _utcnow()
    row = LearnerExplanation(
        account_id=account_id,
        thread_id=payload.thread_id,
        concept_id=concept.id,
        loop_index=payload.loop_index,
        state=payload.state.value,
        raw_text=payload.raw_text,
        claims_json=[claim.model_dump(mode="json") for claim in payload.claims],
        gap_report_json=(
            payload.gap_report.model_dump(mode="json", by_alias=True)
            if payload.gap_report is not None
            else None
        ),
        learner_talk_ratio=(
            _decimal(round(payload.learner_talk_ratio, 3))
            if payload.learner_talk_ratio is not None
            else None
        ),
    )
    db.add(row)
    await db.flush()

    deltas: tuple[MasteryDeltaOut, ...] = ()
    skipped: tuple[str, ...] = ()
    if payload.evidence is not None:
        deltas, skipped = await record_evidence(
            db,
            account_id=account_id,
            concept=concept,
            kind=payload.evidence.kind,
            atom_keys=payload.evidence.atom_keys,
            depth=payload.evidence.depth,
            now=stamp,
        )
    await db.commit()
    await db.refresh(row)
    return _explanation_out(row, concept.slug, mastery=deltas, skipped=skipped)


async def record_evidence(
    db: AsyncSession,
    *,
    account_id: int,
    concept: LearningConcept,
    kind: str,
    atom_keys: tuple[str, ...] | list[str],
    depth: int = 0,
    now: datetime | None = None,
) -> tuple[tuple[MasteryDeltaOut, ...], tuple[str, ...]]:
    """掌握度记账的**唯一**写入口(数值全部来自 :func:`app.pedagogy.mastery.record`)。

    返回 ``(增量列表, 被跳过的 atom_key)``:

    - 不属于本概念的键被跳过并点名返回(静默忽略会让「判分器给错 key」永远查不出来);
    - ``verified=false`` 的 Atom 仍会出现在结果里, 但 ``credited=false`` +
      ``reason`` 说明为什么没加分(vision §9「未溯源不记账」的对外表达);
    - ``credited=false`` 时**不写库**: 数值没变, 且不能移动 ``updated_at``(见模块文档约束 2)。
    """
    stamp = now or _utcnow()
    keys = list(dict.fromkeys(atom_keys))[:EVIDENCE_ATOM_MAX]
    atoms = await _atoms_by_concept(db, concept.id)
    picked = {key: atoms[key] for key in keys if key in atoms}
    skipped = tuple(key for key in keys if key not in atoms)
    if not picked:
        return (), skipped

    cfg = PedagogyConfig.from_settings()
    weights = await atom_weights(db, list(picked.values()))
    ledger = await _ledger_rows(db, account_id, [atom.id for atom in picked.values()])
    entries: list[MasteryEntry] = []
    for key, atom in picked.items():
        weight = weights.get(atom.id, 1.0)
        row = ledger.get(atom.id)
        entries.append(
            _to_entry(row, atom_key=key, concept_slug=concept.slug, weight=weight)
            if row is not None
            else new_entry(key, concept_id=concept.slug, weight=weight, config=cfg)
        )

    updates: tuple[MasteryUpdate, ...] = record_many(
        entries,
        kind,  # type: ignore[arg-type]
        now=stamp,
        verified_atom_ids=[key for key, atom in picked.items() if atom.verified],
        depth=depth,
        config=cfg,
    )
    deltas: list[MasteryDeltaOut] = []
    for update in updates:
        atom = picked[update.entry.atom_id]
        await _persist_update(db, account_id, atom, update, stamp)
        deltas.append(
            MasteryDeltaOut(
                atom_key=update.entry.atom_id,
                before=round(update.before, 4),
                decayed=round(update.decayed, 4),
                after=round(update.after, 4),
                weight=round(update.weight, 4),
                credited=update.credited,
                reason=update.reason,
            )
        )
    return tuple(deltas), skipped


async def _persist_update(
    db: AsyncSession, account_id: int, atom: LearningAtom, update: MasteryUpdate, stamp: datetime
) -> MasteryLedger | None:
    """把一次记账结果落库; ``credited=False`` 时不写(数值原样, 复习时钟不动)。"""
    if not update.credited:
        return None
    entry = update.entry
    statement = select(MasteryLedger).where(
        MasteryLedger.account_id == account_id, MasteryLedger.atom_id == atom.id
    )
    row = (await db.execute(statement)).scalar_one_or_none()
    if row is None:
        row = MasteryLedger(account_id=account_id, atom_id=atom.id)
        db.add(row)
    row.mastery = _decimal(round(entry.mastery, 4))
    row.evidence_count = entry.evidence_count
    row.last_evidence_at = stamp
    row.review_stage = entry.ladder_index
    row.tau_days = _decimal(round(entry.tau_days, 3), "7.000")
    row.depth = entry.depth
    # 排期用 scheduled_review(以 updated_at 为基准), 与「今日复习队列」同一个口径。
    row.next_review_at = scheduled_review(entry)
    row.updated_at = stamp
    await db.flush()
    return row


def _to_entry(
    row: MasteryLedger, *, atom_key: str, concept_slug: str, weight: float
) -> MasteryEntry:
    """``MasteryLedger`` 行 -> 内核账目(整条可还原, 见 models 的设计约束 2)。"""
    return MasteryEntry(
        concept_id=concept_slug,
        atom_id=atom_key,
        mastery=_float(row.mastery),
        weight=weight,
        ladder_index=int(row.review_stage),
        tau_days=_float(row.tau_days, 7.0),
        evidence_count=int(row.evidence_count),
        depth=int(row.depth),
        updated_at=row.updated_at,
    )


async def _ledger_rows(
    db: AsyncSession, account_id: int, atom_ids: list[int]
) -> dict[int, MasteryLedger]:
    if not atom_ids:
        return {}
    statement = select(MasteryLedger).where(
        MasteryLedger.account_id == account_id, MasteryLedger.atom_id.in_(atom_ids)
    )
    return {row.atom_id: row for row in (await db.execute(statement)).scalars().all()}


def _explanation_out(
    row: LearnerExplanation,
    concept_slug: str,
    *,
    mastery: tuple[MasteryDeltaOut, ...] = (),
    skipped: tuple[str, ...] = (),
) -> ExplanationOut:
    claims = row.claims_json or []
    return ExplanationOut(
        id=row.id,
        concept_slug=concept_slug,
        thread_id=row.thread_id,
        loop_index=int(row.loop_index),
        state=row.state,
        raw_text=row.raw_text,
        chars=len(row.raw_text or ""),
        claims=tuple(claims),
        gap_report=row.gap_report_json,
        learner_talk_ratio=_float(row.learner_talk_ratio) if row.learner_talk_ratio is not None else None,
        created_at=row.created_at,
        mastery=mastery,
        skipped_atom_keys=skipped,
    )


async def list_explanations(
    db: AsyncSession,
    account_id: int,
    *,
    concept_slug: str | None = None,
    limit: int = EXPLANATION_LIST_LIMIT,
) -> ExplanationListOut:
    """我的自述列表(倒序)。带 ``concept_slug`` 时只列该概念。"""
    statement = (
        select(LearnerExplanation, LearningConcept.slug)
        .join(LearningConcept, LearningConcept.id == LearnerExplanation.concept_id)
        .where(LearnerExplanation.account_id == account_id)
        .order_by(LearnerExplanation.id.desc())
        .limit(min(max(limit, 1), EXPLANATION_LIST_MAX))
    )
    if concept_slug:
        statement = statement.where(LearningConcept.slug == concept_slug)
    items = [
        ExplanationItem(
            id=row.id,
            concept_slug=slug,
            loop_index=int(row.loop_index),
            state=row.state,
            chars=len(row.raw_text or ""),
            gap_types=_gap_types(row.gap_report_json),
            verdict=_verdict(row.gap_report_json),
            created_at=row.created_at,
        )
        for row, slug in (await db.execute(statement)).all()
    ]
    return ExplanationListOut(count=len(items), items=tuple(items))


def _gap_types(report: Any) -> tuple[str, ...]:
    if not isinstance(report, dict):
        return ()
    gaps = report.get("gaps")
    if not isinstance(gaps, list):
        return ()
    return tuple(str(gap.get("type")) for gap in gaps if isinstance(gap, dict) and gap.get("type"))


def _verdict(report: Any) -> str | None:
    if isinstance(report, dict) and report.get("verdict"):
        return str(report["verdict"])
    return None


async def get_explanation(
    db: AsyncSession, account_id: int, explanation_id: int
) -> ExplanationOut | None:
    """属主校验: 不存在或属于他人一律 ``None``(路由统一 404, 不泄露是否存在)。"""
    statement = (
        select(LearnerExplanation, LearningConcept.slug)
        .join(LearningConcept, LearningConcept.id == LearnerExplanation.concept_id)
        .where(LearnerExplanation.id == explanation_id, LearnerExplanation.account_id == account_id)
    )
    result = (await db.execute(statement)).first()
    if result is None:
        return None
    row, slug = result
    return _explanation_out(row, slug)


# --------------------------------------------------------------------------- 账本与复习


async def _ledger_with_atoms(db: AsyncSession, account_id: int) -> list[tuple[MasteryLedger, LearningAtom, LearningConcept]]:
    statement = (
        select(MasteryLedger, LearningAtom, LearningConcept)
        .join(LearningAtom, LearningAtom.id == MasteryLedger.atom_id)
        .join(LearningConcept, LearningConcept.id == LearningAtom.concept_id)
        .where(MasteryLedger.account_id == account_id)
        .order_by(MasteryLedger.id)
    )
    return [tuple(row) for row in (await db.execute(statement)).all()]  # type: ignore[misc]


async def mastery_list(
    db: AsyncSession, account_id: int, *, now: datetime | None = None
) -> MasteryListOut:
    """我的掌握度账本(含证据计数与复习排期)+ 按概念聚合的加权掌握度。

    ``mastery`` 是**上次记账时**的值(账本原样), ``decayed_mastery`` 是按当下衰减后的值
    —— 前端不该自己算衰减(公式与 τ 都在服务端), 但两个数都要给, 否则「为什么掉了」
    无法解释。概念聚合用衰减后的值, 且把**没有账目的 Atom 记为 0**: 只闭合了 5 个
    Atom 中的 1 个就说「概念掌握 100%」, 对家长是误导。
    """
    stamp = now or _utcnow()
    rows = await _ledger_with_atoms(db, account_id)
    if not rows:
        return MasteryListOut(as_of=stamp, count=0)

    concept_ids = {atom.concept_id for _, atom, _ in rows}
    weights = await _weights_for_concepts(db, concept_ids)
    atoms_of_concept = await _atom_keys_of_concepts(db, concept_ids)

    items: list[MasteryRowOut] = []
    by_concept: dict[int, list[MasteryEntry]] = defaultdict(list)
    recorded: dict[int, int] = defaultdict(int)
    due: dict[int, int] = defaultdict(int)
    for ledger, atom, concept in rows:
        weight = weights.get(atom.id, 1.0)
        entry = _to_entry(
            ledger, atom_key=atom.atom_key, concept_slug=concept.slug, weight=weight
        )
        decayed_value = _decayed(entry, stamp)
        due_at = scheduled_review(entry)
        if due_at is not None and due_at <= stamp:
            due[concept.id] += 1
        items.append(
            MasteryRowOut(
                atom_key=atom.atom_key,
                statement=atom.statement,
                concept_slug=concept.slug,
                concept_title=concept.title,
                verified=bool(atom.verified),
                mastery=round(_float(ledger.mastery), 4),
                weight=weight,
                evidence_count=int(ledger.evidence_count),
                review_stage=int(ledger.review_stage),
                interval_days=interval_days(int(ledger.review_stage)),
                tau_days=_float(ledger.tau_days, 7.0),
                depth=int(ledger.depth),
                decayed_mastery=round(decayed_value, 4),
                next_review_at=ledger.next_review_at,
                last_evidence_at=ledger.last_evidence_at,
                updated_at=ledger.updated_at,
            )
        )
        by_concept[concept.id].append(entry.model_copy(update={"mastery": decayed_value}))
        recorded[concept.id] += 1

    concepts: list[ConceptMasteryOut] = []
    for concept_id, entries in by_concept.items():
        slug = next(c.slug for _, _, c in rows if c.id == concept_id)
        title = next(c.title for _, _, c in rows if c.id == concept_id)
        # 补齐没有账目的 Atom(m=0), 让聚合口径是「覆盖整个概念」而不是「只算学过的」
        accounted = {entry.atom_id for entry in entries}
        for atom_key in atoms_of_concept.get(concept_id, ()):  # noqa: PERF102 - 概念内 Atom 数很小
            if atom_key in accounted:
                continue
            entries.append(new_entry(atom_key, concept_id=slug, weight=1.0))
        concepts.append(
            ConceptMasteryOut(
                concept_slug=slug,
                concept_title=title,
                mastery=round(concept_mastery(entries), 4),
                atoms=len(entries),
                recorded=recorded[concept_id],
                due=due[concept_id],
            )
        )
    concepts.sort(key=lambda item: item.concept_slug)
    return MasteryListOut(as_of=stamp, count=len(items), items=tuple(items), concepts=tuple(concepts))


async def _weights_for_concepts(db: AsyncSession, concept_ids: set[int]) -> dict[int, float]:
    if not concept_ids:
        return {}
    statement = (
        select(LearningAtomEdge.to_atom_id, func.max(LearningAtomEdge.weight))
        .join(LearningAtom, LearningAtom.id == LearningAtomEdge.to_atom_id)
        .where(LearningAtom.concept_id.in_(concept_ids))
        .group_by(LearningAtomEdge.to_atom_id)
    )
    return {atom_id: _float(weight, 1.0) for atom_id, weight in (await db.execute(statement)).all()}


async def _atom_keys_of_concepts(db: AsyncSession, concept_ids: set[int]) -> dict[int, list[str]]:
    if not concept_ids:
        return {}
    statement = select(LearningAtom.concept_id, LearningAtom.atom_key).where(
        LearningAtom.concept_id.in_(concept_ids)
    )
    keys: dict[int, list[str]] = defaultdict(list)
    for concept_id, atom_key in (await db.execute(statement)).all():
        keys[concept_id].append(atom_key)
    return keys


async def review_queue(
    db: AsyncSession,
    account_id: int,
    *,
    limit: int | None = REVIEW_QUEUE_LIMIT,
    now: datetime | None = None,
) -> ReviewQueueOut:
    """今日复习队列: 已到期(``scheduled_review <= now``)的账目, 过期越久排越前。

    裁决完全交给 :func:`app.pedagogy.mastery.review_queue`(纯函数, 有独立单测);
    本函数只负责把账本行翻成 ``MasteryEntry``、再把结果翻回响应模型。
    """
    stamp = now or _utcnow()
    rows = await _ledger_with_atoms(db, account_id)
    if not rows:
        return ReviewQueueOut(as_of=stamp, count=0, limit=limit)
    weights = await _weights_for_concepts(db, {atom.concept_id for _, atom, _ in rows})
    meta = {
        atom.atom_key: (atom.statement, concept.slug) for _, atom, concept in rows
    }
    entries = [
        _to_entry(
            ledger,
            atom_key=atom.atom_key,
            concept_slug=concept.slug,
            weight=weights.get(atom.id, 1.0),
        )
        for ledger, atom, concept in rows
    ]
    capped = min(max(limit, 1), REVIEW_QUEUE_MAX) if limit else None
    due = compute_review_queue(entries, now=stamp, limit=capped)
    items = [
        ReviewItemOut(
            atom_key=item.entry.atom_id,
            statement=meta.get(item.entry.atom_id, ("", ""))[0],
            concept_slug=meta.get(item.entry.atom_id, ("", ""))[1],
            due_at=item.due_at,
            interval_days=item.interval_days,
            overdue_days=round(item.overdue_days, 2),
            mastery=round(_decayed(item.entry, stamp), 4),
            review_stage=item.entry.ladder_index,
        )
        for item in due
    ]
    return ReviewQueueOut(as_of=stamp, count=len(items), limit=capped, items=tuple(items))


async def count_explanations(db: AsyncSession, account_id: int) -> int:
    """我的自述条数(报告页/空状态判断用; 单独一个 COUNT 查询, 不拉全文)。"""
    statement = select(func.count(LearnerExplanation.id)).where(
        LearnerExplanation.account_id == account_id
    )
    return int((await db.execute(statement)).scalar_one())


__all__ = [
    "EVIDENCE_ATOM_MAX",
    "EXPLANATION_LIST_LIMIT",
    "EXPLANATION_LIST_MAX",
    "REVIEW_QUEUE_LIMIT",
    "REVIEW_QUEUE_MAX",
    "atom_out",
    "atom_weights",
    "bind_concept",
    "concept_graph",
    "concept_out",
    "count_explanations",
    "create_explanation",
    "get_concept",
    "get_explanation",
    "list_explanations",
    "mastery_list",
    "record_evidence",
    "review_queue",
]
