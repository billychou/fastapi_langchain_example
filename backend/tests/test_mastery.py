"""掌握度模型的单元测试: 纯函数数值断言 + 单调性(roadmap P0 交付物 #3)。

对应 ``docs/pedagogy-core.md`` §4.3 与 §8 的测试计划:
「掌握度 | 纯函数 | ``m=0.5, w=1.0, α=0.35 → 0.675``; 衰减单调」。

所有时间都显式传入, 因此这些用例是确定性的(不依赖 ``datetime.now``)。
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest
from app.pedagogy import PedagogyConfig
from app.pedagogy.mastery import (
    DEMOTION_STEPS,
    DEPTH_FACTOR_FLOOR,
    EVIDENCE_WEIGHTS,
    NON_CREDITING,
    REVIEW_LADDER_DAYS,
    DepthSummary,
    MasteryEntry,
    MasteryUpdate,
    concept_mastery,
    decay,
    depth_factor,
    depth_summary,
    elapsed_days_since,
    evidence_weight,
    interval_days,
    new_entry,
    next_review,
    record,
    record_many,
    review,
    review_queue,
    scheduled_review,
    update_mastery,
)

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
ALPHA = PedagogyConfig().mastery_alpha  # 0.35


def entry(**overrides: object) -> MasteryEntry:
    base: dict[str, object] = {"concept_id": "concept:entropy", "atom_id": "atom:second-law"}
    base.update(overrides)
    return MasteryEntry.model_validate(base)


# --------------------------------------------------------------------------- 更新公式


def test_update_matches_documented_example() -> None:
    """文档写死的样例: ``m=0.5, w=1.0, α=0.35 → 0.675``(精确相等, 不是近似)。"""
    assert update_mastery(0.5, 1.0, 0.35) == 0.675


def test_update_is_diminishing_returns() -> None:
    """m 越高, 同一条证据带来的增量越小 —— 堵掉「刷几次闭合就满分」。"""
    low = update_mastery(0.1, 1.0, ALPHA) - 0.1
    high = update_mastery(0.9, 1.0, ALPHA) - 0.9
    assert low > high > 0


def test_update_never_exceeds_bounds() -> None:
    assert update_mastery(0.999, 1.0, 1.0) <= 1.0
    assert update_mastery(0.0, 1.0, 1.0) == 1.0
    assert update_mastery(0.4, 0.0, ALPHA) == 0.4  # w=0 不改变掌握度


@pytest.mark.parametrize("alpha", [-0.1, 1.5])
def test_update_rejects_invalid_alpha(alpha: float) -> None:
    with pytest.raises(ValueError, match="alpha"):
        update_mastery(0.5, 1.0, alpha)


def test_evidence_weight_table_matches_doc() -> None:
    """权重表逐字对应 pedagogy-core §4.3。"""
    assert EVIDENCE_WEIGHTS == {
        "closed_explanation": 1.0,
        "transfer_correct": 0.8,
        "probe_answered": 0.4,
        "direct_answer_read": 0.0,
        "escaped": 0.0,
    }
    assert NON_CREDITING == {"direct_answer_read", "escaped"}
    for kind, weight in EVIDENCE_WEIGHTS.items():
        assert evidence_weight(kind) == weight  # 不给深度时不做修正


def test_evidence_weight_applies_depth_correction() -> None:
    """同样一次闭合, Why-chain 问到底的比第二层就停的更值钱(§4.2)。"""
    shallow = evidence_weight("closed_explanation", depth=0, max_depth=5)
    deep = evidence_weight("closed_explanation", depth=5, max_depth=5)
    assert shallow == pytest.approx(DEPTH_FACTOR_FLOOR)
    assert deep == 1.0
    assert evidence_weight("closed_explanation", depth=99, max_depth=5) == 1.0  # 封顶


def test_depth_factor_is_monotonic_and_bounded() -> None:
    factors = [depth_factor(d, 5) for d in range(0, 8)]
    assert factors == sorted(factors)
    assert all(DEPTH_FACTOR_FLOOR <= f <= 1.0 for f in factors)
    assert depth_factor(3, 0) == 1.0  # max_depth 非法时不修正


# --------------------------------------------------------------------------- 衰减


def test_decay_is_monotonic_in_elapsed_time() -> None:
    """衰减单调: 时间越久掌握度越低, 且永远 ≥ 0。"""
    values = [decay(0.8, t, 7.0) for t in range(0, 61)]
    assert values == sorted(values, reverse=True)
    assert values[0] == 0.8
    assert all(0.0 <= v <= 0.8 for v in values)
    assert values[-1] < 0.01


def test_decay_half_life_style_checkpoint() -> None:
    """Δt = τ 时衰减到 1/e ≈ 0.3679(指数遗忘的定义性质)。"""
    assert decay(1.0, 7.0, 7.0) == pytest.approx(math.exp(-1))
    assert decay(0.5, 14.0, 7.0) == pytest.approx(0.5 * math.exp(-2))


def test_decay_ignores_clock_skew() -> None:
    """时钟回拨不得让掌握度凭空增长。"""
    assert decay(0.6, -30.0, 7.0) == 0.6


def test_decay_of_zero_stays_zero() -> None:
    assert decay(0.0, 30.0, 7.0) == 0.0


def test_decay_rejects_non_positive_tau() -> None:
    with pytest.raises(ValueError, match="tau_days"):
        decay(0.5, 1.0, 0.0)


def test_larger_tau_forgets_slower() -> None:
    """τ 越大遗忘越慢(为「τ 由历史表现拟合」留出接口)。"""
    assert decay(0.8, 10.0, 30.0) > decay(0.8, 10.0, 3.0)


def test_elapsed_days_since() -> None:
    assert elapsed_days_since(None, NOW) == 0.0
    assert elapsed_days_since(NOW - timedelta(hours=12), NOW) == pytest.approx(0.5)
    assert elapsed_days_since(NOW + timedelta(days=1), NOW) == 0.0  # 未来时间按 0 算


def test_elapsed_days_treats_naive_datetime_as_utc() -> None:
    naive = datetime(2026, 2, 28, 12, 0)  # 无时区
    assert elapsed_days_since(naive, NOW) == pytest.approx(1.0)


# --------------------------------------------------------------------------- 记账


def test_record_closed_explanation_on_fresh_entry() -> None:
    """全新账目 + 一次闭合: m = 0 + α·1.0·(1−0) = 0.35。"""
    update = record(entry(), "closed_explanation", now=NOW)
    assert isinstance(update, MasteryUpdate)
    assert update.credited is True
    assert update.before == 0.0
    assert update.after == pytest.approx(ALPHA)
    assert update.weight == 1.0
    assert update.entry.evidence_count == 1
    assert update.entry.updated_at == NOW
    assert update.reason  # 记账理由必填(可溯源)


def test_record_decays_before_crediting() -> None:
    """先衰减到当下, 再记增量: 7 天前的 0.8 会先掉到 0.8/e。"""
    stale = entry(mastery=0.8, updated_at=NOW - timedelta(days=7))
    update = record(stale, "probe_answered", now=NOW)
    assert update.decayed == pytest.approx(0.8 * math.exp(-1))
    expected = update.decayed + ALPHA * 0.4 * (1 - update.decayed)
    assert update.after == pytest.approx(expected)
    assert update.before == 0.8


def test_record_weight_ordering() -> None:
    """闭合 > 迁移题 > 追问: 证据越硬, 增量越大。"""
    results = [
        record(entry(mastery=0.3), kind, now=NOW).after
        for kind in ("closed_explanation", "transfer_correct", "probe_answered")
    ]
    assert results[0] > results[1] > results[2]


def test_record_repeated_evidence_converges_below_one() -> None:
    """反复闭合单调上升且有上界(收益递减), 不会一次刷满。"""
    current = entry()
    values = [current.mastery]
    for _ in range(6):
        update = record(current, "closed_explanation", now=NOW)
        current = update.entry
        values.append(current.mastery)
    assert values == sorted(values)
    assert values[-1] < 1.0
    assert current.evidence_count == 6


@pytest.mark.parametrize("kind", sorted(NON_CREDITING))
def test_non_crediting_evidence_changes_nothing(kind: str) -> None:
    """读直答 / 放弃: 不加分, 也不动时间戳(不能把复习时钟往后推)。"""
    original = entry(mastery=0.6, updated_at=NOW - timedelta(days=10), evidence_count=2)
    update = record(original, kind, now=NOW)
    assert update.credited is False
    assert update.after == 0.6
    assert update.entry == original
    assert update.entry.evidence_count == 2
    assert update.entry.updated_at == original.updated_at
    assert update.decayed < 0.6  # 仅供展示: 现在结算会掉到多少
    assert "不产生掌握证据" in update.reason


def test_unverified_atom_is_not_credited() -> None:
    """``verified=false`` 的 Atom 不得参与记账(vision §9 幻觉对策)。"""
    original = entry(mastery=0.4)
    update = record(original, "closed_explanation", now=NOW, verified=False)
    assert update.credited is False
    assert update.entry == original
    assert "未验证" in update.reason


def test_record_uses_config_alpha() -> None:
    cfg = PedagogyConfig(mastery_alpha=0.5)
    update = record(entry(mastery=0.5), "closed_explanation", now=NOW, config=cfg)
    assert update.after == pytest.approx(0.75)


def test_record_depth_lowers_weight() -> None:
    deep = record(entry(), "closed_explanation", now=NOW, depth=5)
    shallow = record(entry(), "closed_explanation", now=NOW, depth=0)
    assert deep.weight == 1.0
    assert shallow.weight == pytest.approx(DEPTH_FACTOR_FLOOR)
    assert deep.after > shallow.after
    assert shallow.entry.depth == 0 and deep.entry.depth == 5


def test_record_many_only_credits_verified_atoms() -> None:
    """白名单外的 Atom 一律按未验证处理, 调用方不需要自己过滤。"""
    entries = [
        entry(atom_id="atom:a", mastery=0.1),
        entry(atom_id="atom:b", mastery=0.1),
        entry(atom_id="atom:c", mastery=0.1),
    ]
    updates = record_many(entries, "closed_explanation", now=NOW, verified_atom_ids=["atom:a", "atom:c"])
    assert [u.credited for u in updates] == [True, False, True]
    assert updates[1].entry == entries[1]
    assert updates[0].after == updates[2].after


def test_record_is_deterministic() -> None:
    """同输入同输出: 账本可回放、可回归。"""
    args = {"now": NOW, "depth": 3}
    first = record(entry(mastery=0.25, updated_at=NOW - timedelta(days=2)), "transfer_correct", **args)
    second = record(entry(mastery=0.25, updated_at=NOW - timedelta(days=2)), "transfer_correct", **args)
    assert first == second


# --------------------------------------------------------------------------- 复习阶梯


def test_ladder_matches_doc() -> None:
    assert REVIEW_LADDER_DAYS == (1, 3, 7, 21, 60)
    assert DEMOTION_STEPS == 2


@pytest.mark.parametrize(
    ("index", "days"), [(-5, 1), (0, 1), (1, 3), (2, 7), (3, 21), (4, 60), (9, 60)]
)
def test_interval_days_clamps_to_ladder(index: int, days: int) -> None:
    assert interval_days(index) == days


def test_entry_interval_property() -> None:
    assert entry(ladder_index=2).interval_days == 7


def test_next_review_adds_interval_to_now() -> None:
    assert next_review(entry(ladder_index=0), now=NOW) == NOW + timedelta(days=1)
    assert next_review(entry(ladder_index=4), now=NOW) == NOW + timedelta(days=60)


def test_scheduled_review_anchors_on_updated_at() -> None:
    assert scheduled_review(entry()) is None  # 没记过账 -> 没有复习义务
    stamped = entry(ladder_index=2, updated_at=NOW)
    assert scheduled_review(stamped) == NOW + timedelta(days=7)


def test_review_closed_promotes_one_step() -> None:
    step = review(entry(ladder_index=1, updated_at=NOW), closed=True, now=NOW)
    assert step.promoted is True
    assert step.demoted is False
    assert step.reenter_teach is False
    assert step.entry.ladder_index == 2
    assert step.interval_days == 7
    assert step.due_at == NOW + timedelta(days=7)


def test_review_closed_caps_at_top_step() -> None:
    step = review(entry(ladder_index=len(REVIEW_LADDER_DAYS) - 1), closed=True, now=NOW)
    assert step.promoted is False
    assert step.entry.ladder_index == len(REVIEW_LADDER_DAYS) - 1
    assert "最高阶" in step.reason


def test_review_broken_demotes_two_steps_and_reenters_teach() -> None:
    step = review(entry(ladder_index=3, mastery=0.7), closed=False, now=NOW)
    assert step.demoted is True
    assert step.reenter_teach is True
    assert step.entry.ladder_index == 1
    assert step.interval_days == 3
    assert step.due_at == NOW + timedelta(days=3)


def test_review_broken_floors_at_zero() -> None:
    step = review(entry(ladder_index=1), closed=False, now=NOW)
    assert step.entry.ladder_index == 0
    assert step.interval_days == 1


def test_review_does_not_change_mastery_value() -> None:
    """阶梯只动排期; 掌握度数值的唯一入口是 ``record``(账本可审计)。"""
    for closed in (True, False):
        step = review(entry(mastery=0.42, ladder_index=2), closed=closed, now=NOW)
        assert step.entry.mastery == 0.42


def test_repeated_reviews_are_monotonic() -> None:
    """连续闭合升阶、连续断裂降阶, 且都夹在阶梯范围内。"""
    current = entry(ladder_index=0)
    indices = []
    for _ in range(8):
        current = review(current, closed=True, now=NOW).entry
        indices.append(current.ladder_index)
    assert indices == sorted(indices)
    assert indices[-1] == len(REVIEW_LADDER_DAYS) - 1

    broken = []
    for _ in range(8):
        current = review(current, closed=False, now=NOW).entry
        broken.append(current.ladder_index)
    assert broken == sorted(broken, reverse=True)
    assert broken[-1] == 0


# --------------------------------------------------------------------------- 复习队列


def test_review_queue_selects_and_orders_due_entries() -> None:
    overdue = entry(atom_id="atom:old", ladder_index=0, updated_at=NOW - timedelta(days=5))
    due_today = entry(atom_id="atom:today", ladder_index=0, updated_at=NOW - timedelta(days=1))
    future = entry(atom_id="atom:future", ladder_index=4, updated_at=NOW - timedelta(days=1))
    fresh = entry(atom_id="atom:fresh")  # 从没记过账

    queue = review_queue([future, due_today, overdue, fresh], now=NOW)
    assert [item.entry.atom_id for item in queue] == ["atom:old", "atom:today"]
    assert queue[0].overdue_days == pytest.approx(4.0)
    assert queue[1].overdue_days == pytest.approx(0.0)
    assert queue[0].due_at < queue[1].due_at


def test_review_queue_respects_limit() -> None:
    entries = [
        entry(atom_id=f"atom:{i}", updated_at=NOW - timedelta(days=2 + i)) for i in range(5)
    ]
    assert len(review_queue(entries, now=NOW, limit=2)) == 2
    assert len(review_queue(entries, now=NOW, limit=0)) == 5  # 0/None 表示不限制
    assert len(review_queue(entries, now=NOW)) == 5


def test_review_queue_empty() -> None:
    assert review_queue([], now=NOW) == ()


# --------------------------------------------------------------------------- 概念掌握度


def test_concept_mastery_is_weighted_mean() -> None:
    entries = [
        entry(atom_id="a", mastery=1.0, weight=3.0),
        entry(atom_id="b", mastery=0.0, weight=1.0),
    ]
    assert concept_mastery(entries) == pytest.approx(0.75)


def test_concept_mastery_edge_cases() -> None:
    assert concept_mastery([]) == 0.0
    assert concept_mastery([entry(mastery=0.42)]) == pytest.approx(0.42)
    # 权重越高影响越大
    heavy = concept_mastery(
        [entry(atom_id="a", mastery=0.9, weight=9.0), entry(atom_id="b", mastery=0.1, weight=1.0)]
    )
    assert heavy > 0.5


# --------------------------------------------------------------------------- 理解深度指标


def test_depth_summary_average_and_distribution() -> None:
    summary = depth_summary(
        depths=[1, 3, 5], landings=["definition", "axiom", "derivable", "observable"]
    )
    assert isinstance(summary, DepthSummary)
    assert summary.samples == 4
    assert summary.average_depth == pytest.approx(3.0)
    assert summary.landing == {"definition": 1, "axiom": 1, "derivable": 1, "observable": 1}
    assert summary.terminal_ratio == pytest.approx(0.75)  # derivable 不是可停点


def test_depth_summary_all_derivable() -> None:
    """全都停在 derivable -> 落点可停点比例为 0(说明还没问到底)。"""
    summary = depth_summary(depths=[2, 2], landings=["derivable", "derivable"])
    assert summary.terminal_ratio == 0.0


def test_depth_summary_empty() -> None:
    summary = depth_summary()
    assert summary.samples == 0
    assert summary.average_depth == 0.0
    assert summary.landing == {}
    assert summary.terminal_ratio == 0.0

# --------------------------------------------------------------------------- 账目工厂


def test_new_entry_uses_coldstart_tau_from_config() -> None:
    """冷启动 τ 来自配置而不是硬编码(roadmap §0 原则 4)。"""
    cfg = PedagogyConfig(mastery_coldstart_tau_days=21.0)
    fresh = new_entry("atom:entropy", concept_id="concept:entropy", config=cfg)
    assert fresh.tau_days == 21.0
    assert fresh.mastery == 0.0
    assert fresh.ladder_index == 0
    assert fresh.evidence_count == 0
    assert fresh.updated_at is None  # 还没有证据


def test_new_entry_defaults() -> None:
    fresh = new_entry("atom:entropy")
    assert fresh.tau_days == PedagogyConfig().mastery_coldstart_tau_days == 7.0
    assert fresh.weight == 1.0


def test_new_entry_stays_out_of_review_queue_until_credited() -> None:
    """刚建、还没记账的 Atom 不该被塞进复习队列(它该走正常循环)。"""
    fresh = new_entry("atom:entropy")
    assert scheduled_review(fresh) is None
    assert review_queue([fresh], now=NOW) == ()
    credited = record(fresh, "closed_explanation", now=NOW).entry
    assert review_queue([credited], now=NOW + timedelta(days=1)) != ()
