"""卡点检测器(判分层)的单元测试: 假判分器 + 规则裁决, 零网络零密钥。

对应 ``docs/education-agent-roadmap.md`` P0 交付物 #2 与 ``docs/pedagogy-core.md`` §2、§3。

覆盖策略: **四类断裂每类至少两个正/反样例** —— 这一层是判分一致率(Cohen's κ)
的直接来源, 规则改动必须能被回归测试抓住, 而不是靠「手感」。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from app.pedagogy import (
    Atom,
    Claim,
    FeynmanState,
    PedagogyConfig,
)
from app.pedagogy.gaps import (
    CAUSAL_CONNECTIVES,
    DEFAULT_PROBES,
    GENERIC_JARGON_PROBE,
    GRADER_TEMPERATURE,
    Detection,
    GapDetector,
    GradeResult,
    GradingError,
    LlmGrader,
    build_prompt,
    completeness_of,
    derive_gaps,
    has_causal_connective,
)

CFG = PedagogyConfig()

# --------------------------------------------------------------------------- 夹具 Atom

#: 可停点(定义): 熵本身
ATOM_ENTROPY = Atom(
    id="atom:entropy",
    concept_id="concept:entropy",
    statement="熵是孤立系统无序程度的度量",
    kind="definition",
    verified=True,
)
#: 公理: 依赖 atom:entropy, 用来验证「缺的那条边」推断
ATOM_SECOND_LAW = Atom(
    id="atom:second-law",
    concept_id="concept:entropy",
    statement="孤立系统的总熵不会减少",
    kind="axiom",
    depends_on=("atom:entropy",),
    verified=True,
)
#: 未验证 Atom: 不得参与 completeness 记账(vision §9 幻觉对策)
ATOM_UNVERIFIED = Atom(
    id="atom:stat-mech",
    concept_id="concept:entropy",
    statement="熵与微观状态数的对数成正比",
    kind="derivable",
    verified=False,
)

ATOMS = (ATOM_ENTROPY, ATOM_SECOND_LAW, ATOM_UNVERIFIED)


class FakeGrader:
    """假判分器: 返回预先写好的 JSON, 并记录提示词以便断言。"""

    def __init__(self, payload: dict[str, Any] | str | Exception) -> None:
        self.payload = payload
        self.calls: list[tuple[str, str]] = []

    async def grade(self, *, system: str, user: str) -> str:
        self.calls.append((system, user))
        if isinstance(self.payload, Exception):
            raise self.payload
        if isinstance(self.payload, dict):
            return json.dumps(self.payload, ensure_ascii=False)
        return self.payload

    @property
    def call_count(self) -> int:
        return len(self.calls)


def grade(**overrides: Any) -> dict[str, Any]:
    """一份「什么都没检出」的判分基线, 用例只覆盖自己关心的字段。"""
    base: dict[str, Any] = {
        "claims": [],
        "jargon_terms": [],
        "analogy": None,
        "counterexample": None,
        "own_words": 0.9,
        "boundary_awareness": 0.9,
        "reason": "基线判分",
    }
    base.update(overrides)
    return base


def claim(
    claim_id: str = "c1",
    text: str = "然后它就变大了",
    *,
    status: str = "unsupported",
    gap_type: str | None = None,
    atom_ref: str | None = None,
    note: str = "",
) -> dict[str, Any]:
    return {
        "id": claim_id,
        "text": text,
        "status": status,
        "gap_type": gap_type,
        "atom_ref": atom_ref,
        "note": note,
    }


def gaps_of(gaps: Any, gap_type: str) -> list[Any]:
    return [g for g in gaps if g.type == gap_type]


# --------------------------------------------------------------------------- 1. 术语盾


def test_jargon_shield_flags_unexpanded_term() -> None:
    """样例 1: 用「熵」解释「熵增」, 术语从未被展开 -> 术语盾(严重度 3)。"""
    gaps = derive_gaps(
        claims=[Claim(id="c1", text="这是因为熵增加了", status="jargon_shield")],
        raw_text="杯子凉了是因为熵增加了。",
        jargon_terms=["熵"],
        atoms=ATOMS,
        config=CFG,
    )
    shields = gaps_of(gaps, "jargon_shield")
    assert len(shields) == 1
    assert shields[0].severity == 3
    assert shields[0].claim_id == "c1"
    assert "熵" in shields[0].probe
    assert shields[0].probe == DEFAULT_PROBES["jargon_shield"].format(term="熵")


def test_jargon_shield_absent_when_term_is_expanded() -> None:
    """样例 2: 同一段话里已经把术语讲清楚(supported 且明显更长) -> 不算术语盾。"""
    gaps = derive_gaps(
        claims=[
            Claim(id="c1", text="熵是系统无序程度的度量，越大越乱", status="supported",
                  atom_ref="atom:entropy"),
            Claim(id="c2", text="所以凉咖啡让总熵变大", status="supported",
                  atom_ref="atom:second-law"),
        ],
        raw_text="熵是系统无序程度的度量，越大越乱，所以凉咖啡让总熵变大。",
        jargon_terms=["熵"],
        atoms=ATOMS,
        config=CFG,
    )
    assert gaps == ()


def test_jargon_shield_expanded_via_atom_statement() -> None:
    """术语的展开也可以由「supported claim 指向的定义 Atom」证明。"""
    gaps = derive_gaps(
        claims=[Claim(id="c1", text="看熵", status="supported", atom_ref="atom:entropy")],
        raw_text="看熵就行了。",
        jargon_terms=["熵"],
        atoms=ATOMS,
        config=CFG,
    )
    assert gaps == ()


def test_jargon_shield_ignores_terms_absent_from_raw_text() -> None:
    """判分器交出的术语若不在学习者原话里, 不能凭空记账(防幻觉)。"""
    gaps = derive_gaps(
        claims=[Claim(id="c1", text="热量从热传到冷", status="supported")],
        raw_text="热量从热传到冷。",
        jargon_terms=["自由能"],
        config=CFG,
    )
    assert gaps == ()


def test_jargon_shield_deduplicates_repeated_terms() -> None:
    """同一个术语出现多次只记一个卡点, 避免把一个盾算成三个。"""
    gaps = derive_gaps(
        claims=[Claim(id="c1", text="焓在变化", status="unsupported")],
        raw_text="焓变了，焓就是那样。",
        jargon_terms=["焓", "焓", ""],
        config=CFG,
    )
    assert len(gaps_of(gaps, "jargon_shield")) == 1


def test_jargon_shield_from_claim_status_without_terms() -> None:
    """判分器只标了 ``status=jargon_shield`` 却没给术语 -> 仍记账, 追问退化为大白话。"""
    gaps = derive_gaps(
        claims=[Claim(id="c7", text="这就是耗散结构的作用", status="jargon_shield")],
        raw_text="这就是耗散结构的作用。",
        config=CFG,
    )
    shields = gaps_of(gaps, "jargon_shield")
    assert len(shields) == 1
    assert shields[0].claim_id == "c7"
    assert shields[0].probe == GENERIC_JARGON_PROBE


def test_jargon_shield_claim_not_double_counted_with_term() -> None:
    """术语与 claim 指向同一处时只记一次(去重靠 claim_id)。"""
    gaps = derive_gaps(
        claims=[Claim(id="c1", text="熵在增加", status="jargon_shield")],
        raw_text="熵在增加。",
        jargon_terms=["熵"],
        config=CFG,
    )
    assert len(gaps_of(gaps, "jargon_shield")) == 1


# --------------------------------------------------------------------------- 2. 因果断链


def test_causal_gap_from_connective_in_claim() -> None:
    """样例 1: 步骤对但「然后就」跳过理由 -> 因果断链, 并推断缺的那条边。"""
    gaps = derive_gaps(
        claims=[
            Claim(
                id="c1",
                text="温度降低，然后熵就变大了",
                status="unsupported",
                atom_ref="atom:second-law",
            )
        ],
        raw_text="温度降低，然后熵就变大了。",
        atoms=ATOMS,
        config=CFG,
    )
    causal = gaps_of(gaps, "causal_gap")
    assert len(causal) == 1
    assert causal[0].claim_id == "c1"
    assert causal[0].probe == DEFAULT_PROBES["causal_gap"]
    assert causal[0].missing_edge is not None
    assert causal[0].missing_edge.model_dump(by_alias=True) == {
        "from": "atom:entropy",
        "to": "atom:second-law",
    }
    # 补给包: 先补缺失的前置 Atom, 再补被引用的 Atom, 且受字数预算约束
    assert causal[0].supply is not None
    assert [a.id for a in causal[0].supply.atoms] == ["atom:entropy", "atom:second-law"]
    assert causal[0].supply.max_chars <= CFG.return_max_chars


def test_causal_gap_from_connective_in_raw_text_only() -> None:
    """样例 2: 连接词出现在原话别处也算信号(claim 拆句会切碎连接词)。"""
    gaps = derive_gaps(
        claims=[Claim(id="c1", text="冰会融化", status="unsupported")],
        raw_text="冰会融化，因此水温会下降。",
        config=CFG,
    )
    assert len(gaps_of(gaps, "causal_gap")) == 1


def test_no_causal_gap_without_connective() -> None:
    """没有「然后就 / 所以」这类信号时不硬判因果断链(避免过度诊断)。"""
    gaps = derive_gaps(
        claims=[Claim(id="c1", text="冰会融化", status="unsupported")],
        raw_text="冰会融化。",
        config=CFG,
    )
    assert gaps == ()


def test_causal_gap_respects_grader_classification() -> None:
    """判分器已把该 claim 归到别的类 -> 尊重它, 不再叠加因果断链。"""
    gaps = derive_gaps(
        claims=[
            Claim(
                id="c1",
                text="所以它就是那样",
                status="unsupported",
                gap_type="no_boundary",
            )
        ],
        raw_text="所以它就是那样。",
        require_boundary=True,
        config=CFG,
    )
    assert gaps_of(gaps, "causal_gap") == []
    assert len(gaps_of(gaps, "no_boundary")) == 1


def test_causal_gap_uses_claim_note_as_probe() -> None:
    """判分器给了具体追问就用它 —— 诊断必须可执行, 但不必是模板句。"""
    gaps = derive_gaps(
        claims=[
            Claim(
                id="c1",
                text="然后就得到了结论",
                status="unsupported",
                note="为什么温度降低会让总熵增加？",
            )
        ],
        raw_text="然后就得到了结论。",
        config=CFG,
    )
    assert gaps[0].probe == "为什么温度降低会让总熵增加？"


def test_wrong_claim_is_not_a_causal_gap() -> None:
    """``wrong`` 是与已验证 Atom 冲突, 走 wrong_claim_ids 通道, 不算因果断链。"""
    gaps = derive_gaps(
        claims=[Claim(id="c1", text="所以熵会减少", status="wrong")],
        raw_text="所以熵会减少。",
        config=CFG,
    )
    assert gaps == ()


@pytest.mark.parametrize("word", CAUSAL_CONNECTIVES)
def test_every_causal_connective_is_detected(word: str) -> None:
    assert has_causal_connective(f"前面{word}后面") is True


def test_supply_budget_is_clamped_by_config() -> None:
    """``return_max_chars`` 调小时, 补给包字数预算随之收紧(护栏可配置)。"""
    tight = PedagogyConfig(return_max_chars=120)
    gaps = derive_gaps(
        claims=[Claim(id="c1", text="然后就成立了", status="unsupported", atom_ref="atom:second-law")],
        raw_text="然后就成立了。",
        atoms=ATOMS,
        config=tight,
    )
    assert gaps[0].supply is not None
    assert gaps[0].supply.max_chars == 120


def test_supply_is_none_when_atom_ref_unknown() -> None:
    """引用了不存在的 Atom -> 不给补给包(宁缺毋滥, 不能编 Atom)。"""
    gaps = derive_gaps(
        claims=[Claim(id="c1", text="然后就成立了", status="unsupported", atom_ref="atom:ghost")],
        raw_text="然后就成立了。",
        atoms=ATOMS,
        config=CFG,
    )
    assert gaps[0].supply is None
    assert gaps[0].missing_edge is None


def test_causal_gap_signal_b_recited_step_without_prerequisite() -> None:
    """信号 B: 流程背对了(supported), 但前置 Atom 本轮从未讲到 -> DAG 上缺一条边。"""
    gaps = derive_gaps(
        claims=[Claim(id="c1", text="所以总熵不会减少", status="supported", atom_ref="atom:second-law")],
        raw_text="咖啡凉了，所以总熵不会减少。",
        atoms=ATOMS,
        config=CFG,
    )
    causal = gaps_of(gaps, "causal_gap")
    assert len(causal) == 1
    assert causal[0].severity == 1  # 比「直接说不出理由」轻一级
    assert causal[0].missing_edge is not None
    assert causal[0].missing_edge.model_dump(by_alias=True) == {
        "from": "atom:entropy",
        "to": "atom:second-law",
    }
    # 补给顺序: 先补没讲通的前置, 再补它引用的 Atom
    assert [a.id for a in causal[0].supply.atoms] == ["atom:entropy", "atom:second-law"]


def test_causal_gap_signal_b_suppressed_when_prerequisite_covered() -> None:
    """前置已经被另一条 supported claim 讲通 -> 不缺边, 不记因果断链。"""
    gaps = derive_gaps(
        claims=[
            Claim(id="c1", text="熵是无序程度的度量", status="supported", atom_ref="atom:entropy"),
            Claim(id="c2", text="所以总熵不会减少", status="supported", atom_ref="atom:second-law"),
        ],
        raw_text="熵是无序程度的度量，所以总熵不会减少。",
        atoms=ATOMS,
        config=CFG,
    )
    assert gaps == ()


def test_causal_gap_signal_b_needs_a_known_atom() -> None:
    """引用的 Atom 不在清单里 -> 无从判边, 不硬判(宁缺毋滥)。"""
    gaps = derive_gaps(
        claims=[Claim(id="c1", text="所以它成立", status="supported", atom_ref="atom:ghost")],
        raw_text="所以它成立。",
        atoms=ATOMS,
        config=CFG,
    )
    assert gaps == ()


def test_causal_gap_signal_b_needs_connective() -> None:
    """没有「然后就 / 所以」这类信号时, 缺边本身不算因果断链(精度优先)。"""
    gaps = derive_gaps(
        claims=[Claim(id="c1", text="总熵不会减少", status="supported", atom_ref="atom:second-law")],
        raw_text="总熵不会减少。",
        atoms=ATOMS,
        config=CFG,
    )
    assert gaps == ()


def test_supply_skips_already_covered_prerequisite() -> None:
    """补给包不重复讲已经讲通的 Atom —— 预算要花在缺口上。"""
    gaps = derive_gaps(
        claims=[
            Claim(id="c1", text="熵是无序程度的度量", status="supported", atom_ref="atom:entropy"),
            Claim(
                id="c2",
                text="然后就得到了总熵不减",
                status="unsupported",
                atom_ref="atom:second-law",
            ),
        ],
        raw_text="熵是无序程度的度量，然后就得到了总熵不减。",
        atoms=ATOMS,
        config=CFG,
    )
    causal = gaps_of(gaps, "causal_gap")
    assert len(causal) == 1
    assert causal[0].missing_edge is None  # 前置已讲通, 缺的是理由而不是边
    assert [a.id for a in causal[0].supply.atoms] == ["atom:second-law"]


# --------------------------------------------------------------------------- 3. 类比失配


def test_broken_analogy_when_flagged() -> None:
    """样例 1: 有类比且被判失配 -> 追问「这个类比在哪里失效」。"""
    gaps = derive_gaps(
        claims=[Claim(id="c1", text="熵就像房间变乱", status="supported")],
        raw_text="熵就像房间变乱。",
        analogy="房间自己会变乱",
        analogy_flagged=True,
        config=CFG,
    )
    broken = gaps_of(gaps, "broken_analogy")
    assert len(broken) == 1
    assert broken[0].probe == DEFAULT_PROBES["broken_analogy"]


def test_broken_analogy_from_claim_gap_type() -> None:
    """样例 2: 判分器把某条 claim 归为 broken_analogy, 等价于「已判失配」。"""
    gaps = derive_gaps(
        claims=[
            Claim(
                id="c2",
                text="熵像水流一样会流走",
                status="unsupported",
                gap_type="broken_analogy",
                atom_ref="atom:entropy",
                note="水会流走，但熵不会被消耗",
            )
        ],
        raw_text="熵像水流一样会流走。",
        analogy="熵像水",
        atoms=ATOMS,
        config=CFG,
    )
    broken = gaps_of(gaps, "broken_analogy")
    assert len(broken) == 1
    assert broken[0].claim_id == "c2"
    assert broken[0].probe == "水会流走，但熵不会被消耗"
    assert broken[0].supply is not None


def test_no_analogy_gap_without_analogy() -> None:
    """根本没有类比时不判类比失配(即使调用方误标 flagged)。"""
    gaps = derive_gaps(
        claims=[Claim(id="c1", text="熵会增加", status="supported")],
        raw_text="熵会增加。",
        analogy=None,
        analogy_flagged=True,
        config=CFG,
    )
    assert gaps == ()


def test_unflagged_analogy_is_not_a_gap() -> None:
    """类比存在但没被判失配 -> 不记卡点(好的类比是资产)。"""
    gaps = derive_gaps(
        claims=[Claim(id="c1", text="熵像房间变乱", status="supported")],
        raw_text="熵像房间变乱。",
        analogy="房间自己会变乱",
        config=CFG,
    )
    assert gaps == ()


# --------------------------------------------------------------------------- 4. 边界缺失


def test_no_boundary_when_counterexample_missing() -> None:
    """样例 1: Simplify 阶段要反例, 学习者给不出 -> 边界缺失。"""
    gaps = derive_gaps(
        claims=[Claim(id="c1", text="熵总是增加", status="supported")],
        raw_text="熵总是增加。",
        counterexample=None,
        require_boundary=True,
        config=CFG,
    )
    boundary = gaps_of(gaps, "no_boundary")
    assert len(boundary) == 1
    assert boundary[0].probe == DEFAULT_PROBES["no_boundary"]


def test_no_boundary_not_judged_outside_simplify() -> None:
    """样例 2: Detect 阶段不要求反例 -> 不判边界缺失(否则第一圈永远闭合不了)。"""
    gaps = derive_gaps(
        claims=[Claim(id="c1", text="熵总是增加", status="supported")],
        raw_text="熵总是增加。",
        counterexample=None,
        require_boundary=False,
        config=CFG,
    )
    assert gaps == ()


def test_no_boundary_is_not_duplicated_per_claim() -> None:
    """一个反例都没给时, 整体只记一次边界缺失, 不按 claim 重复叠加。"""
    gaps = derive_gaps(
        claims=[
            Claim(id="c1", text="熵总是增加", status="unsupported", gap_type="no_boundary"),
            Claim(id="c2", text="任何系统都如此", status="unsupported", gap_type="no_boundary"),
        ],
        raw_text="熵总是增加，任何系统都如此。",
        counterexample="   ",
        require_boundary=True,
        config=CFG,
    )
    boundary = gaps_of(gaps, "no_boundary")
    assert len(boundary) == 1
    assert boundary[0].claim_id == "c1"


def test_boundary_gap_per_claim_when_counterexample_exists() -> None:
    """给了反例但某条命题仍缺适用条件 -> 精确追问那一条。"""
    gaps = derive_gaps(
        claims=[
            Claim(
                id="c1",
                text="熵在任何过程里都增加",
                status="unsupported",
                gap_type="no_boundary",
                note="可逆过程里总熵不变，这句话什么时候不成立？",
            )
        ],
        raw_text="熵在任何过程里都增加。",
        counterexample="理想可逆过程熵不变",
        require_boundary=True,
        config=CFG,
    )
    boundary = gaps_of(gaps, "no_boundary")
    assert len(boundary) == 1
    assert boundary[0].claim_id == "c1"
    assert boundary[0].probe == "可逆过程里总熵不变，这句话什么时候不成立？"


def test_counterexample_without_flagged_claims_yields_no_gap() -> None:
    """给了反例、也没有命题被标边界缺失 -> 干净通过。"""
    gaps = derive_gaps(
        claims=[Claim(id="c1", text="孤立系统熵不减", status="supported")],
        raw_text="孤立系统熵不减。",
        counterexample="开放系统可以局部减熵",
        require_boundary=True,
        config=CFG,
    )
    assert gaps == ()


def test_derive_gaps_on_empty_evidence() -> None:
    assert derive_gaps(claims=[], raw_text="", config=CFG) == ()


# --------------------------------------------------------------------------- completeness


def test_completeness_counts_required_atom_coverage() -> None:
    """客观维度: 按必要 Atom 覆盖率算, 只有 supported 才算覆盖。"""
    claims = [
        Claim(id="c1", text="熵是无序度", status="supported", atom_ref="atom:entropy"),
        Claim(id="c2", text="所以总熵不减", status="unsupported", atom_ref="atom:second-law"),
    ]
    assert completeness_of(claims, ["atom:entropy", "atom:second-law"]) == 0.5


def test_completeness_full_coverage() -> None:
    claims = [
        Claim(id="c1", text="熵是无序度", status="supported", atom_ref="atom:entropy"),
        Claim(id="c2", text="总熵不减", status="supported", atom_ref="atom:second-law"),
    ]
    assert completeness_of(claims, ["atom:entropy", "atom:second-law"]) == 1.0


def test_completeness_falls_back_to_supported_ratio() -> None:
    """没有必要 Atom 清单时退化为 supported 占比, 而不是采信模型自报。"""
    claims = [
        Claim(id="c1", text="A", status="supported"),
        Claim(id="c2", text="B", status="vague"),
        Claim(id="c3", text="C", status="wrong"),
        Claim(id="c4", text="D", status="supported"),
    ]
    assert completeness_of(claims) == 0.5
    assert completeness_of([]) == 0.0


# --------------------------------------------------------------------------- 解析判分输出


async def test_analyze_parses_fenced_json() -> None:
    payload = "```json\n" + json.dumps(grade(reason="围栏输出"), ensure_ascii=False) + "\n```"
    grader = FakeGrader(payload)
    detection = await GapDetector(grader, config=CFG).analyze(
        explanation="熵是无序度，孤立系统总熵不减。", concept_id="concept:entropy"
    )
    assert detection.report.scores.reason == "围栏输出"


async def test_analyze_parses_json_wrapped_in_prose() -> None:
    payload = "好的，判分如下：\n" + json.dumps(grade(reason="夹在废话里"), ensure_ascii=False) + "\n希望有帮助"
    detection = await GapDetector(FakeGrader(payload), config=CFG).analyze(
        explanation="熵是无序度。", concept_id="concept:entropy"
    )
    assert detection.report.scores.reason == "夹在废话里"


async def test_analyze_raises_on_non_json() -> None:
    """判分不可信时**向上抛**, 不静默降级 —— 宁可不转移, 不能凭半个报告加分。"""
    with pytest.raises(GradingError):
        await GapDetector(FakeGrader("我觉得他讲得还行"), config=CFG).analyze(
            explanation="熵是无序度。", concept_id="concept:entropy"
        )


async def test_analyze_raises_on_empty_output() -> None:
    with pytest.raises(GradingError):
        await GapDetector(FakeGrader("   "), config=CFG).analyze(
            explanation="x", concept_id="concept:entropy"
        )


async def test_analyze_raises_on_invalid_schema() -> None:
    """own_words 超出 0-1 -> 结构不合法, 抛错而不是截断。"""
    with pytest.raises(GradingError):
        await GapDetector(FakeGrader(grade(own_words=5)), config=CFG).analyze(
            explanation="熵是无序度。", concept_id="concept:entropy"
        )


async def test_analyze_sanitizes_unknown_enum_values() -> None:
    """非法 status / gap_type 被规整为 vague / None, 而不是让整份判分报废。"""
    payload = grade(
        claims=[
            claim("c1", "差不多就是那种感觉", status="kinda_ok", gap_type="weird_gap"),
            claim("c2", "supported 不该带 gap_type", status="supported", gap_type="causal_gap"),
        ]
    )
    detection = await GapDetector(FakeGrader(payload), config=CFG).analyze(
        explanation="差不多就是那种感觉。", concept_id="concept:entropy"
    )
    claims = {c.id: c for c in detection.turn.claims}
    assert claims["c1"].status == "vague"
    assert claims["c1"].gap_type is None
    assert claims["c2"].status == "supported"
    assert claims["c2"].gap_type is None


async def test_analyze_drops_invalid_claims_and_autofills_ids() -> None:
    payload = grade(
        claims=[
            {"text": "   "},  # 空文本 -> 丢弃
            "不是字典",  # 非字典 -> 丢弃
            {"text": "熵是无序度", "status": "supported"},  # 缺 id -> 自动补
        ]
    )
    detection = await GapDetector(FakeGrader(payload), config=CFG).analyze(
        explanation="熵是无序度。", concept_id="concept:entropy"
    )
    assert [c.id for c in detection.turn.claims] == ["c1"]


# --------------------------------------------------------------------------- analyze 端到端


async def test_analyze_detect_finds_jargon_shield_and_causal_gap() -> None:
    """一次检测同时命中两类卡点: 报告必须逐条给出可执行追问。"""
    payload = grade(
        claims=[
            claim("c1", "因为熵在增加", status="jargon_shield", atom_ref="atom:entropy"),
            claim("c2", "然后温度就平衡了", status="unsupported", atom_ref="atom:second-law"),
        ],
        jargon_terms=["熵"],
        own_words=0.4,
        reason="术语未展开且跳过理由",
    )
    detection = await GapDetector(FakeGrader(payload), config=CFG).analyze(
        explanation="因为熵在增加，然后温度就平衡了。",
        concept_id="concept:entropy",
        atoms=ATOMS,
        thread_id="t-1",
        account_id=7,
        learner_chars=20,
        system_chars=20,
        explanation_id="ex-1",
    )
    assert isinstance(detection, Detection)
    types = [g.type for g in detection.report.gaps]
    assert types == ["jargon_shield", "causal_gap"]
    assert all(g.probe for g in detection.report.gaps)
    assert detection.report.verdict == "not_closed"
    assert detection.report.explanation_id == "ex-1"
    assert detection.turn.thread_id == "t-1"
    assert detection.turn.account_id == 7
    assert detection.turn.chars == len("因为熵在增加，然后温度就平衡了。")


async def test_analyze_verdict_closed_when_no_gaps() -> None:
    payload = grade(
        claims=[
            claim("c1", "熵是系统无序程度的度量", status="supported", atom_ref="atom:entropy"),
            claim("c2", "孤立系统总熵不会减少", status="supported", atom_ref="atom:second-law"),
        ],
        counterexample="开放系统可以局部减熵",
        reason="两条必要 Atom 都讲到了",
    )
    detection = await GapDetector(FakeGrader(payload), config=CFG).analyze(
        explanation="熵是系统无序程度的度量，孤立系统总熵不会减少。",
        concept_id="concept:entropy",
        atoms=ATOMS,
        state=FeynmanState.SIMPLIFY,
    )
    assert detection.report.gaps == ()
    assert detection.report.verdict == "closed"
    assert detection.report.scores.completeness == 1.0
    assert detection.report.wrong_claim_ids == ()


async def test_analyze_completeness_ignores_unverified_atoms() -> None:
    """未验证 Atom 不进 completeness 分母 —— 否则模型编的 Atom 会稀释掌握度。"""
    payload = grade(
        claims=[
            claim("c1", "熵是无序度", status="supported", atom_ref="atom:entropy"),
            claim("c2", "总熵不减", status="supported", atom_ref="atom:second-law"),
        ]
    )
    detection = await GapDetector(FakeGrader(payload), config=CFG).analyze(
        explanation="熵是无序度，总熵不减。", concept_id="concept:entropy", atoms=ATOMS
    )
    assert detection.report.scores.completeness == 1.0


async def test_analyze_completeness_falls_back_without_atoms() -> None:
    payload = grade(
        claims=[
            claim("c1", "熵是无序度", status="supported"),
            claim("c2", "反正就是那样", status="vague"),
        ]
    )
    detection = await GapDetector(FakeGrader(payload), config=CFG).analyze(
        explanation="熵是无序度，反正就是那样。", concept_id="concept:entropy"
    )
    assert detection.report.scores.completeness == 0.5


async def test_analyze_collects_wrong_claim_ids() -> None:
    payload = grade(
        claims=[
            claim("c1", "熵会减少", status="wrong", atom_ref="atom:second-law"),
            claim("c2", "熵是无序度", status="supported", atom_ref="atom:entropy"),
        ]
    )
    detection = await GapDetector(FakeGrader(payload), config=CFG).analyze(
        explanation="熵会减少，熵是无序度。", concept_id="concept:entropy", atoms=ATOMS
    )
    assert detection.report.wrong_claim_ids == ("c1",)
    assert detection.report.verdict == "not_closed"


async def test_analyze_requires_boundary_only_in_simplify() -> None:
    """边界要求由状态推导: Detect 不判, Simplify 判。"""
    payload = grade(claims=[claim("c1", "熵总是增加", status="supported")])
    detector = GapDetector(FakeGrader(payload), config=CFG)
    detect = await detector.analyze(
        explanation="熵总是增加。", concept_id="concept:entropy", state=FeynmanState.DETECT
    )
    simplify = await detector.analyze(
        explanation="熵总是增加。", concept_id="concept:entropy", state=FeynmanState.SIMPLIFY
    )
    assert gaps_of(detect.report.gaps, "no_boundary") == []
    assert len(gaps_of(simplify.report.gaps, "no_boundary")) == 1
    assert simplify.turn.state is FeynmanState.SIMPLIFY


async def test_analyze_computes_learner_talk_ratio() -> None:
    payload = grade(claims=[claim("c1", "十个字的讲解呀", status="supported")])
    detection = await GapDetector(FakeGrader(payload), config=CFG).analyze(
        explanation="十个字的讲解呀",  # 7 字
        concept_id="concept:entropy",
        learner_chars=3,
        system_chars=10,
    )
    assert detection.report.learner_talk_ratio == 0.5  # (3+7) / (3+7+10)


async def test_analyze_reason_fallback_when_missing() -> None:
    detection = await GapDetector(FakeGrader(grade(reason="")), config=CFG).analyze(
        explanation="熵是无序度。", concept_id="concept:entropy"
    )
    assert detection.report.scores.reason == "(判分器未给出理由)"


async def test_analyze_passes_prompt_to_grader() -> None:
    grader = FakeGrader(grade())
    await GapDetector(grader, config=CFG).analyze(
        explanation="熵是无序度。",
        concept_id="concept:entropy",
        atoms=(ATOM_ENTROPY,),
        state=FeynmanState.SIMPLIFY,
    )
    assert grader.call_count == 1
    system, user = grader.calls[0]
    assert "学习诊断器" in system
    assert "concept:entropy" in user
    assert "atom:entropy [definition] 熵是孤立系统无序程度的度量" in user
    assert "是否需要反例/边界: 是" in user
    assert "熵是无序度。" in user


async def test_analyze_uses_injected_prompt_builder() -> None:
    """提示词构造器可注入: 学科化定制不需要改内核。"""
    seen: dict[str, Any] = {}

    def builder(**kwargs: Any) -> tuple[str, str]:
        seen.update(kwargs)
        return "SYS", "USR"

    grader = FakeGrader(grade())
    await GapDetector(grader, config=CFG, prompt_builder=builder).analyze(
        explanation="熵是无序度。", concept_id="concept:entropy", atoms=(ATOM_ENTROPY,)
    )
    assert grader.calls == [("SYS", "USR")]
    assert seen["concept_id"] == "concept:entropy"
    assert seen["require_boundary"] is False
    assert tuple(seen["atoms"]) == (ATOM_ENTROPY,)


async def test_analyze_propagates_grader_exception() -> None:
    with pytest.raises(RuntimeError, match="模型超时"):
        await GapDetector(FakeGrader(RuntimeError("模型超时")), config=CFG).analyze(
            explanation="熵是无序度。", concept_id="concept:entropy"
        )


# --------------------------------------------------------------------------- 提示词


def test_build_prompt_lists_atoms_and_boundary_flag() -> None:
    system, user = build_prompt(
        explanation="学习者原话",
        concept_id="concept:entropy",
        atoms=(ATOM_ENTROPY, ATOM_SECOND_LAW),
        require_boundary=True,
    )
    # 判定口径写在系统提示里(而不是让调用方口头约定), 这样 rubric 才可回归
    for token in ("supported", "unsupported", "wrong", "jargon_shield", "vague", "own_words"):
        assert token in system
    assert "atom:second-law [axiom] 孤立系统的总熵不会减少" in user
    assert "是否需要反例/边界: 是" in user
    assert '"""\n学习者原话\n"""' in user


def test_build_prompt_without_atoms() -> None:
    _, user = build_prompt(explanation="x", concept_id="concept:c")
    assert "(暂无已验证 Atom)" in user
    assert "是否需要反例/边界: 否" in user


def test_grade_result_ignores_extra_fields_and_defaults() -> None:
    result = GradeResult.model_validate({"unexpected": 1})
    assert result.claims == []
    assert result.own_words == 0.0
    assert result.analogy is None


# --------------------------------------------------------------------------- LlmGrader


class StubChatModel:
    """最小可用的 BaseChatModel 替身: 支持 bind / ainvoke。"""

    def __init__(self, content: Any) -> None:
        self.content = content
        self.bound: dict[str, Any] = {}
        self.messages: Any = None

    def bind(self, **kwargs: Any) -> StubChatModel:
        self.bound = kwargs
        return self

    async def ainvoke(self, messages: Any) -> SimpleNamespace:
        self.messages = messages
        return SimpleNamespace(content=self.content)


async def test_llm_grader_binds_zero_temperature() -> None:
    model = StubChatModel('{"reason":"ok"}')
    grader = LlmGrader(model)  # type: ignore[arg-type]
    out = await grader.grade(system="SYS", user="USR")
    assert model.bound == {"temperature": GRADER_TEMPERATURE}
    assert out == '{"reason":"ok"}'
    assert [type(m).__name__ for m in model.messages] == ["SystemMessage", "HumanMessage"]


async def test_llm_grader_flattens_multimodal_content() -> None:
    model = StubChatModel([{"type": "text", "text": '{"a":'}, "raw", {"other": 1}])
    out = await LlmGrader(model).grade(system="S", user="U")  # type: ignore[arg-type]
    assert out == '{"a":raw'


async def test_llm_grader_handles_none_content() -> None:
    model = StubChatModel(None)
    assert await LlmGrader(model).grade(system="S", user="U") == ""  # type: ignore[arg-type]


async def test_llm_grader_output_feeds_detector() -> None:
    """判分器与检测器串起来: 模型输出 -> 证据 -> 卡点。"""
    model = StubChatModel(
        json.dumps(
            grade(
                claims=[claim("c1", "因为焓在变", status="jargon_shield")],
                jargon_terms=["焓"],
                reason="术语盾",
            ),
            ensure_ascii=False,
        )
    )
    detection = await GapDetector(LlmGrader(model), config=CFG).analyze(  # type: ignore[arg-type]
        explanation="因为焓在变化。", concept_id="concept:enthalpy"
    )
    assert [g.type for g in detection.report.gaps] == ["jargon_shield"]
