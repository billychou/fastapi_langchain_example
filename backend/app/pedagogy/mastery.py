"""掌握度模型: 更新 / 遗忘衰减 / 间隔复习(见 ``docs/pedagogy-core.md`` §4.3)。

设计约束(为什么长这样):

1. **纯函数、可读时钟**: 时间只从参数 ``now`` 进来。不传 ``now`` 时取
   ``datetime.now(timezone.utc)``, 而新账目(``updated_at=None``)的衰减恒为 0,
   所以「不传 now」的用例依然是确定性的 —— 这是 roadmap §8「掌握度: 纯函数,
   数值断言 + 单调性」能成立的前提。
2. **记账只有一个入口**: 掌握度数值只会被 :func:`record` 改动; 复习升/降阶只动
   阶梯位置, 不动数值。账本因此可审计: 每一个数字都能追到一条证据。
3. **不做复杂模型**: 冷启动不上 FSRS 这类需要大量标注数据才能拟合的算法
   (pedagogy-core §4.3 明确「不做」), 但接口隔离在本模块, 之后可整体替换。
4. **未验证 Atom 不记账**: ``verified=false`` 的 Atom 只能用于提示(vision §9
   幻觉对策), 传进来会得到 ``credited=False`` 而不是抛异常 —— 聊天链路不该 500。

公式(与文档逐字对应)::

    证据权重 w: closed_explanation=1.0, transfer_correct=0.8, probe_answered=0.4,
                direct_answer_read=0.0, escaped=0.0
    更新:      m ← m + α · w · (1 − m)                 # α 默认 0.35
    衰减:      m ← m · exp(−Δt / τ)                    # τ 冷启动 7 天
    概念掌握度: M = Σ m(edge) · weight(edge) / Σ weight(edge)
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.pedagogy.policy import PedagogyConfig
from app.pedagogy.schemas import TERMINAL_ATOM_KINDS, AtomKind

#: 证据类型: 只有「学习者自己产出」的东西才算证据。
EvidenceKind = Literal[
    "closed_explanation",  # 一次讲解闭合(费曼循环走完)
    "transfer_correct",  # 迁移题做对
    "probe_answered",  # 回答了一次追问
    "direct_answer_read",  # 读了直答(逃生舱)
    "escaped",  # 中途放弃
]

#: 证据权重(pedagogy-core §4.3 的表格, 逐字实现)。
EVIDENCE_WEIGHTS: dict[EvidenceKind, float] = {
    "closed_explanation": 1.0,
    "transfer_correct": 0.8,
    "probe_answered": 0.4,
    "direct_answer_read": 0.0,
    "escaped": 0.0,
}

#: 不产生掌握证据的类型: 读答案与放弃都**不加分**(护栏 ``escape_hatch`` 的口径)。
NON_CREDITING: frozenset[str] = frozenset({"direct_answer_read", "escaped"})

#: 间隔重复阶梯(天): 1 / 3 / 7 / 21 / 60。
REVIEW_LADDER_DAYS: tuple[int, ...] = (1, 3, 7, 21, 60)

#: 复习时断裂 -> 降两阶并重新进入 ``Teach``。
DEMOTION_STEPS = 2

#: 一天的秒数(衰减用天为单位, 落库用秒精度)。
SECONDS_PER_DAY = 86400.0

#: 理解深度修正的下限: Why-chain 深度为 0 时, 证据权重打七折。
#: 「能下探到定义/公理的人与在第二层卡住的人掌握度不应相同」(§4.2)的 P0 实现;
#: 这个常数是**待校准**的, 校准依据是 P0 #10 的 learner simulator, 不是拍脑袋。
DEPTH_FACTOR_FLOOR = 0.7


# --------------------------------------------------------------------------- 账目


class MasteryEntry(BaseModel):
    """一条 Atom 的掌握度账目(纯数据, 不含派生字段)。"""

    model_config = ConfigDict(frozen=True)

    concept_id: str = Field(default="", max_length=191)
    atom_id: str = Field(min_length=1, max_length=191)
    #: 掌握度 m ∈ [0,1]
    mastery: float = Field(default=0.0, ge=0.0, le=1.0)
    #: 边权重: 关键边(前置多、被依赖多)权重更高, 决定概念掌握度的分母
    weight: float = Field(default=1.0, gt=0.0)
    #: 复习阶梯位置(索引 :data:`REVIEW_LADDER_DAYS`)
    ladder_index: int = Field(default=0, ge=0)
    #: 遗忘时间常数 τ(天), 冷启动值来自 ``PEDAGOGY_MASTERY_COLDSTART_TAU_DAYS``;
    #: 这里的 default 只是反序列化旧账目时的兜底, 新建请走 :func:`new_entry`。
    tau_days: float = Field(default=7.0, gt=0.0)
    #: 记过多少条**有效**证据(不记账的不算)
    evidence_count: int = Field(default=0, ge=0)
    #: 最近一次 Why-chain 下探深度
    depth: int = Field(default=0, ge=0)
    #: 最近一次记账时间(由持久化层写入; 纯函数只读它算 Δt)
    updated_at: datetime | None = None

    @property
    def interval_days(self) -> int:
        """当前阶梯对应的复习间隔(天)。"""
        return interval_days(self.ladder_index)


def new_entry(
    atom_id: str,
    *,
    concept_id: str = "",
    weight: float = 1.0,
    config: PedagogyConfig | None = None,
) -> MasteryEntry:
    """新建一条账目: 掌握度 0、阶梯 0、τ 取配置的**冷启动值**。

    刻意提供工厂函数而不是让调用方直接 ``MasteryEntry(...)``: 冷启动 τ 必须来自
    ``PEDAGOGY_*`` 配置(roadmap §0 原则 4「阈值不写死在代码里」)。
    ``updated_at`` 保持 ``None`` —— 还没有证据, 因此也不该出现在复习队列里。
    """
    cfg = config or PedagogyConfig()
    return MasteryEntry(
        concept_id=concept_id,
        atom_id=atom_id,
        weight=weight,
        tau_days=cfg.mastery_coldstart_tau_days,
    )


class MasteryUpdate(BaseModel):
    """一次记账的结果 —— 直接对应 SSE ``learn`` 事件里的 ``mastery`` 载荷。"""

    model_config = ConfigDict(frozen=True)

    entry: MasteryEntry
    before: float = Field(ge=0.0, le=1.0)
    #: 衰减到当下、但还没记增量时的值(用于解释「为什么先掉了一点」)。
    #: 不记账时这个值**不落库**, 只供日志与前端展示「如果现在结算会掉到多少」。
    decayed: float = Field(ge=0.0, le=1.0)
    after: float = Field(ge=0.0, le=1.0)
    #: 实际使用的 w(含理解深度修正)
    weight: float = Field(ge=0.0)
    credited: bool = True
    reason: str = ""


class ReviewStep(BaseModel):
    """一次复习排期/结算的结果。"""

    model_config = ConfigDict(frozen=True)

    entry: MasteryEntry
    interval_days: int = Field(ge=0)
    due_at: datetime | None = None
    promoted: bool = False
    demoted: bool = False
    #: 断裂 -> 需要重新进入 ``Teach``(状态机转移由调用方执行, 本模块不碰状态机)
    reenter_teach: bool = False
    reason: str = ""


class ReviewDue(BaseModel):
    """复习队列里的一项。"""

    model_config = ConfigDict(frozen=True)

    entry: MasteryEntry
    due_at: datetime
    interval_days: int = Field(ge=0)
    overdue_days: float = Field(default=0.0, ge=0.0)


class DepthSummary(BaseModel):
    """理解深度指标 = 平均下探深度 + 落点类型分布(pedagogy-core §4.2)。"""

    model_config = ConfigDict(frozen=True)

    samples: int = Field(default=0, ge=0)
    average_depth: float = Field(default=0.0, ge=0.0)
    #: 落点类型分布: ``{"definition": 3, "derivable": 1, ...}``
    landing: dict[str, int] = Field(default_factory=dict)
    #: 落到**可停点**(定义/公理/可观察)的比例; 落在 ``derivable`` 说明还没问到底
    terminal_ratio: float = Field(default=0.0, ge=0.0, le=1.0)


# --------------------------------------------------------------------------- 数学


def evidence_weight(
    kind: EvidenceKind,
    *,
    depth: int | None = None,
    max_depth: int = 5,
) -> float:
    """证据权重 w, 可选按 Why-chain 深度修正。

    ``depth`` 为 ``None`` 时不修正(与文档公式逐字一致); 给了深度就乘以
    :func:`depth_factor` —— 同样一次闭合, 问到底的和第二层就停的不等值。
    """
    base = EVIDENCE_WEIGHTS[kind]
    if depth is None:
        return base
    return base * depth_factor(depth, max_depth)


def depth_factor(depth: int, max_depth: int = 5) -> float:
    """深度修正系数: ``[DEPTH_FACTOR_FLOOR, 1.0]`` 之间线性上升, 超过上限即封顶。"""
    if max_depth <= 0:
        return 1.0
    ratio = min(max(depth, 0) / max_depth, 1.0)
    return DEPTH_FACTOR_FLOOR + (1.0 - DEPTH_FACTOR_FLOOR) * ratio


def decay(mastery: float, elapsed_days: float, tau_days: float) -> float:
    """遗忘衰减 ``m ← m · exp(−Δt / τ)``。

    单调性保证: Δt 越大 m 越小, 且永远 ≥ 0; 时钟回拨(Δt < 0)按 0 处理 ——
    掌握度不会因为服务器时间跳变而凭空增长。
    """
    if tau_days <= 0:
        raise ValueError("tau_days 必须为正数")
    if mastery <= 0.0:
        return 0.0
    elapsed = max(elapsed_days, 0.0)
    return min(max(mastery * math.exp(-elapsed / tau_days), 0.0), 1.0)


def update_mastery(mastery: float, weight: float, alpha: float) -> float:
    """指数加权更新 ``m ← m + α · w · (1 − m)``, 结果夹在 [0,1]。

    这个形式的好处: m 越接近 1, 同样的证据带来的增量越小(收益递减),
    因此不会出现「刷几次闭合就满分」的漏洞。
    """
    if not 0.0 <= alpha <= 1.0:
        raise ValueError("alpha 必须在 [0,1] 内")
    delta = alpha * max(weight, 0.0) * (1.0 - min(max(mastery, 0.0), 1.0))
    return min(max(mastery + delta, 0.0), 1.0)


def elapsed_days_since(updated_at: datetime | None, now: datetime | None = None) -> float:
    """从上次记账到现在过了多少天(不足一天按小数算)。"""
    if updated_at is None:
        return 0.0
    return max((_as_utc(now or _utcnow()) - _as_utc(updated_at)).total_seconds(), 0.0) / SECONDS_PER_DAY


def interval_days(ladder_index: int) -> int:
    """阶梯位置 -> 间隔天数; 越界自动夹到阶梯两端(最高阶就停在 60 天)。"""
    if not REVIEW_LADDER_DAYS:  # pragma: no cover - 常量不可能为空
        return 0
    index = min(max(ladder_index, 0), len(REVIEW_LADDER_DAYS) - 1)
    return REVIEW_LADDER_DAYS[index]


def concept_mastery(entries: Sequence[MasteryEntry]) -> float:
    """概念掌握度 ``M = Σ m·weight / Σ weight``; 没有账目时为 0.0。"""
    total_weight = sum(entry.weight for entry in entries)
    if total_weight <= 0:
        return 0.0
    return sum(entry.mastery * entry.weight for entry in entries) / total_weight


# --------------------------------------------------------------------------- 记账


def record(
    entry: MasteryEntry,
    kind: EvidenceKind,
    *,
    now: datetime | None = None,
    verified: bool = True,
    depth: int | None = None,
    config: PedagogyConfig | None = None,
) -> MasteryUpdate:
    """记一条证据: 先按 Δt 衰减到当下, 再按 w 增量更新。

    三种情况**不记账**(``credited=False``, 数值原样保留, 只刷新时间戳):

    - ``kind`` 属于 :data:`NON_CREDITING`(读直答 / 放弃) —— 没有产生掌握证据;
    - ``verified=False`` —— 未溯源的 Atom 不得参与记账(vision §9);
    - ``w = 0``。

    刻意返回结果而不是抛异常: 这些情况在正常教学流程里都会发生(逃生舱就是设计
    的一部分), 让 ``/api/chat`` 变成 500 是不可接受的。
    """
    cfg = config or PedagogyConfig()
    stamp = now or _utcnow()
    elapsed = elapsed_days_since(entry.updated_at, stamp)
    before = entry.mastery
    decayed = decay(before, elapsed, entry.tau_days)
    weight = evidence_weight(kind, depth=depth, max_depth=cfg.why_chain_max_depth)

    if not verified:
        return _no_credit(
            entry, decayed, weight, "Atom 未验证(verified=false), 不参与掌握度记账"
        )
    if kind in NON_CREDITING or weight <= 0.0:
        return _no_credit(entry, decayed, weight, f"{kind} 不产生掌握证据(w={weight:.2f})")

    after = update_mastery(decayed, weight, cfg.mastery_alpha)
    updated = entry.model_copy(
        update={
            "mastery": after,
            "evidence_count": entry.evidence_count + 1,
            "depth": depth if depth is not None else entry.depth,
            "updated_at": stamp,
        }
    )
    return MasteryUpdate(
        entry=updated,
        before=before,
        decayed=decayed,
        after=after,
        weight=weight,
        credited=True,
        reason=(
            f"{kind}: m {before:.3f} →(衰减 {elapsed:.1f} 天) {decayed:.3f} →(w={weight:.2f}, "
            f"α={cfg.mastery_alpha:.2f}) {after:.3f}"
        ),
    )


def record_many(
    entries: Sequence[MasteryEntry],
    kind: EvidenceKind,
    *,
    now: datetime | None = None,
    verified_atom_ids: Iterable[str] = (),
    depth: int | None = None,
    config: PedagogyConfig | None = None,
) -> tuple[MasteryUpdate, ...]:
    """批量记账(一次循环闭合通常涉及多条 Atom 边)。

    ``verified_atom_ids`` 是白名单: 不在里面的 Atom 一律按未验证处理, 不记账。
    调用方因此不需要自己过滤, 也不会因为漏过滤而污染账本。
    """
    verified = set(verified_atom_ids)
    return tuple(
        record(
            entry,
            kind,
            now=now,
            verified=entry.atom_id in verified,
            depth=depth,
            config=config,
        )
        for entry in entries
    )


def _no_credit(
    entry: MasteryEntry, decayed: float, weight: float, reason: str
) -> MasteryUpdate:
    """不记账: 账目**原样返回**(数值、证据计数、时间戳都不动)。

    两个理由:

    - 衰减只依赖总 Δt(``m·exp(−Δt/τ)``), 下次真正记账时一次算清即可, 结果等价,
      没必要为一次无效证据写库;
    - 不移动 ``updated_at`` 就不会把复习时钟往后推 —— 学习者读了一次直答,
      不该让本该今天复习的概念延期。
    """
    return MasteryUpdate(
        entry=entry,
        before=entry.mastery,
        decayed=decayed,
        after=entry.mastery,
        weight=weight,
        credited=False,
        reason=reason,
    )


# --------------------------------------------------------------------------- 复习调度


def next_review(entry: MasteryEntry, *, now: datetime | None = None) -> datetime:
    """下次复习时间 = ``now + interval(m)``(间隔重复阶梯)。

    用于「刚刚记完账/刚结算完复习」的当场排期; 查历史账目是否到期请用
    :func:`scheduled_review`(它以 ``updated_at`` 为基准)。
    """
    return _as_utc(now or _utcnow()) + timedelta(days=entry.interval_days)


def scheduled_review(entry: MasteryEntry) -> datetime | None:
    """这条账目**应该在什么时刻复习** = ``updated_at + interval``。

    从没记过账(``updated_at is None``)的新 Atom 返回 ``None`` —— 它还没有复习
    义务, 应该走正常的费曼循环而不是被塞进复习队列。
    """
    if entry.updated_at is None:
        return None
    return _as_utc(entry.updated_at) + timedelta(days=entry.interval_days)


def review(
    entry: MasteryEntry,
    *,
    closed: bool,
    now: datetime | None = None,
) -> ReviewStep:
    """结算一次复习: 闭合升一阶, 断裂降两阶并重新进入 ``Teach``。

    只动阶梯位置, **不动掌握度数值** —— 数值变化的唯一入口是 :func:`record`
    (复习中产生的证据由调用方按类型记账), 这样账本上每个数字都追得到证据。
    """
    stamp = _as_utc(now or _utcnow())
    if closed:
        index = min(entry.ladder_index + 1, len(REVIEW_LADDER_DAYS) - 1)
        promoted = index != entry.ladder_index
        updated = entry.model_copy(update={"ladder_index": index, "updated_at": stamp})
        reason = (
            f"复习闭合: 阶梯 {entry.ladder_index} → {index}"
            f"(间隔 {updated.interval_days} 天)"
            if promoted
            else f"复习闭合: 已在最高阶({index}), 间隔保持 {updated.interval_days} 天"
        )
        return ReviewStep(
            entry=updated,
            interval_days=updated.interval_days,
            due_at=next_review(updated, now=stamp),
            promoted=promoted,
            reason=reason,
        )

    index = max(entry.ladder_index - DEMOTION_STEPS, 0)
    updated = entry.model_copy(update={"ladder_index": index, "updated_at": stamp})
    return ReviewStep(
        entry=updated,
        interval_days=updated.interval_days,
        due_at=next_review(updated, now=stamp),
        demoted=True,
        reenter_teach=True,
        reason=(
            f"复习断裂: 阶梯 {entry.ladder_index} → {index}(降 {DEMOTION_STEPS} 阶), "
            "需重新进入 Teach 讲一遍"
        ),
    )


def review_queue(
    entries: Iterable[MasteryEntry],
    *,
    now: datetime | None = None,
    limit: int | None = None,
) -> tuple[ReviewDue, ...]:
    """今日复习队列: 已到期(:func:`scheduled_review` ``<= now``)的账目, 按到期时间升序。

    过期越久的排越前; ``limit`` 用于前端「今天先复习 N 个」的口径。
    """
    stamp = _as_utc(now or _utcnow())
    due: list[ReviewDue] = []
    for entry in entries:
        due_at = scheduled_review(entry)
        if due_at is None:
            continue  # 从没记过账 -> 不进复习队列
        if due_at <= stamp:
            due.append(
                ReviewDue(
                    entry=entry,
                    due_at=due_at,
                    interval_days=entry.interval_days,
                    overdue_days=(stamp - due_at).total_seconds() / SECONDS_PER_DAY,
                )
            )
    due.sort(key=lambda item: (item.due_at, item.entry.concept_id, item.entry.atom_id))
    return tuple(due[:limit]) if limit and limit > 0 else tuple(due)


# --------------------------------------------------------------------------- 理解深度


def depth_summary(
    depths: Sequence[int] = (), landings: Sequence[AtomKind | str] = ()
) -> DepthSummary:
    """理解深度指标: 平均下探深度 + 落点类型分布 + 落到可停点的比例。

    这是「同样闭合, 深度不同」的度量口径, 也是 :func:`depth_factor` 的校准依据。
    """
    landing: dict[str, int] = {}
    for kind in landings:
        landing[str(kind)] = landing.get(str(kind), 0) + 1
    terminal = sum(count for kind, count in landing.items() if kind in TERMINAL_ATOM_KINDS)
    samples = len(landings)
    return DepthSummary(
        samples=samples,
        average_depth=(sum(depths) / len(depths)) if depths else 0.0,
        landing=landing,
        terminal_ratio=(terminal / samples) if samples else 0.0,
    )


# --------------------------------------------------------------------------- 时间工具


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    """naive datetime 一律按 UTC 解释, 避免混用本地时区导致衰减算错。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
