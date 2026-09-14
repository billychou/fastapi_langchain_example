"""卡点 -> persona 的推荐映射(``app/pedagogy/persona.py``)。

persona 一律是 ``backend/skills/<name>/`` 里的技能(pedagogy-core §5: 教学法可版本化、
可 A/B), 所以这里推荐的是**技能目录名**, 模型拿到名字后用 ``load_skill`` 自己读 SOP。

两组断言:

1. 映射本身: 四类卡点全覆盖、优先级确定(严重度 > 状态 > 教练兜底)、结果稳定;
2. **与教练手册对账**(本文件最要紧的一条): ``skills/feynman-tutor/SKILL.md`` 的
   「什么时候把话筒交给别的 persona」三段说明必须与内核推荐逐对一致。手册说「术语盾切
   小白同学」而内核推荐了别人, 模型会同时收到两套互相矛盾的编排指令, 而且不会报错。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest
from app.pedagogy.persona import (
    CHALLENGER,
    DRILL,
    LISTENER,
    PERSONA_BY_GAP,
    PERSONA_BY_STATE,
    TUTOR,
    recommend_persona,
)
from app.pedagogy.schemas import Atom, AtomBundle, GapItem, GapType, PedagogyState
from app.pedagogy.states import FeynmanState
from app.skills import SkillRegistry
from app.tools import ALL_TOOLS

SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"
SWITCH_SECTION = "## 什么时候把话筒交给别的 persona"
#: 手册里「→ 切 `persona`」的写法
_SWITCH_TARGET = re.compile(r"切\s*`([a-z0-9-]+)`")


@pytest.fixture(scope="module")
def registry() -> SkillRegistry:
    reg = SkillRegistry(SKILLS_DIR, known_tools={tool.name for tool in ALL_TOOLS})
    reg.reload()
    return reg


def gap(gap_type: str, severity: int = 1, probe: str = "为什么?") -> GapItem:
    return GapItem(
        type=gap_type,  # type: ignore[arg-type]
        claim_id="c1",
        probe=probe,
        severity=severity,
        supply=AtomBundle(atoms=(Atom(id="atom:a", statement="A"),), max_chars=200),
    )


def state_at(phase: FeynmanState, *gaps: GapItem) -> PedagogyState:
    return PedagogyState(
        thread_id="thread-persona",
        account_id=7,
        learner_id=7,
        concept_id="闭包",
        state=phase,
        loop_index=1,
        open_gaps=gaps,
    )


def switch_bullets(registry: SkillRegistry) -> list[str]:
    """手册里那几条「- 需要… → 切 `persona`」的说明(含缩进的续行)。"""
    tutor = registry.get(TUTOR)
    assert tutor is not None, "教练技能不存在"
    section = tutor.body.split(SWITCH_SECTION, 1)[1].split("\n## ", 1)[0]
    bullets: list[str] = []
    current: list[str] = []
    def flush() -> None:
        if current:
            bullets.append("\n".join(current))

    for line in section.splitlines():
        if line.startswith("- 需要"):
            flush()
            current = [line]
        elif line.startswith("- "):
            # 同级的其他条目(不是切换说明): 结束当前 bullet, 不再收续行
            flush()
            current = []
        elif current:
            current.append(line)
    flush()
    return bullets


# --------------------------------------------------------------------------- 映射完整性


def test_every_gap_type_has_a_persona():
    """四类卡点必须全覆盖: 少一类就退回教练, 相当于对那种断裂没有针对性问法。"""
    assert set(PERSONA_BY_GAP) == set(get_args(GapType))


def test_expected_pairings():
    assert PERSONA_BY_GAP["jargon_shield"] == LISTENER
    assert PERSONA_BY_GAP["causal_gap"] == DRILL
    assert PERSONA_BY_GAP["broken_analogy"] == CHALLENGER
    assert PERSONA_BY_GAP["no_boundary"] == CHALLENGER


def test_personas_are_real_skills(registry: SkillRegistry):
    """推荐的必须是能 ``load_skill`` 的技能目录名, 不是提示词片段或拼错的字符串。"""
    for persona in {TUTOR, *PERSONA_BY_GAP.values(), *PERSONA_BY_STATE.values()}:
        assert registry.get(persona) is not None, f"persona {persona} 不是已加载的技能"
        assert (SKILLS_DIR / persona / "SKILL.md").is_file()


def test_tutor_is_the_default_persona():
    assert TUTOR == "feynman-tutor"
    assert recommend_persona(state_at(FeynmanState.PICK)) == TUTOR
    assert recommend_persona(state_at(FeynmanState.TEACH)) == TUTOR
    assert recommend_persona(state_at(FeynmanState.DETECT)) == TUTOR


def test_simplify_prefers_the_challenger():
    """``Simplify`` 要的是类比/反例/边界, 天然是抬杠同学的活。"""
    assert PERSONA_BY_STATE[FeynmanState.SIMPLIFY] == CHALLENGER
    assert recommend_persona(state_at(FeynmanState.SIMPLIFY)) == CHALLENGER


@pytest.mark.parametrize("phase", [FeynmanState.CLOSED, FeynmanState.ESCAPED])
def test_terminal_states_stay_with_the_tutor(phase: FeynmanState):
    assert recommend_persona(state_at(phase)) == TUTOR


# --------------------------------------------------------------------------- 优先级


def test_gap_outranks_state():
    """卡点是证据驱动的(「他断在哪」比「流程走到哪」更能决定语气)。"""
    assert recommend_persona(state_at(FeynmanState.SIMPLIFY, gap("causal_gap"))) == DRILL


def test_highest_severity_gap_wins():
    state = state_at(
        FeynmanState.DETECT, gap("no_boundary", severity=1), gap("jargon_shield", severity=3)
    )
    assert recommend_persona(state) == LISTENER


def test_same_severity_falls_back_to_a_stable_order():
    """同严重度按内核排序稳定: 同一份状态不能在两轮里推荐出不同的人。"""
    forward = state_at(FeynmanState.DETECT, gap("causal_gap", 2), gap("no_boundary", 2))
    backward = state_at(FeynmanState.DETECT, gap("no_boundary", 2), gap("causal_gap", 2))
    assert recommend_persona(forward) == recommend_persona(backward) == DRILL


def test_recommendation_is_deterministic():
    state = state_at(FeynmanState.RETURN, gap("broken_analogy", 2))
    assert {recommend_persona(state) for _ in range(5)} == {CHALLENGER}


def test_unmapped_gap_does_not_override_the_state():
    """映射表里没有的卡点类型(理论上不会出现)不该抢走状态推荐。"""
    state = state_at(FeynmanState.SIMPLIFY, gap("causal_gap", 1))
    assert recommend_persona(state) == DRILL
    assert recommend_persona(state_at(FeynmanState.SIMPLIFY)) == CHALLENGER


# --------------------------------------------------------------------------- 与手册对账


def test_manual_documents_every_gap_persona_pair(registry: SkillRegistry):
    bullets = switch_bullets(registry)
    assert bullets, f"手册里解析不到「{SWITCH_SECTION}」的切换说明(格式被改坏了?)"
    for gap_type, persona in PERSONA_BY_GAP.items():
        bullet = next((item for item in bullets if f"`{gap_type}`" in item), None)
        assert bullet is not None, f"手册没有说明 {gap_type} 该切谁"
        assert f"切 `{persona}`" in bullet, (
            f"手册与内核不一致: {gap_type} 手册写「{bullet.splitlines()[0]}」, "
            f"内核推荐 {persona}"
        )


def test_manual_does_not_recommend_unknown_personas(registry: SkillRegistry):
    """反向对账: 手册里点名的 persona 必须是内核真的会推荐的技能。"""
    named = {
        match for bullet in switch_bullets(registry) for match in _SWITCH_TARGET.findall(bullet)
    }
    assert named, "手册里没有「切 `persona`」的写法(格式被改坏了?)"
    known = {TUTOR, *PERSONA_BY_GAP.values(), *PERSONA_BY_STATE.values()}
    assert named <= known, f"手册推荐了内核不认的 persona: {sorted(named - known)}"
    for persona in named:
        assert registry.get(persona) is not None


def test_state_based_personas_appear_in_the_switch_section(registry: SkillRegistry):
    """按状态推荐的人也要在手册的切换说明里出现过(否则模型不知道它是谁)。"""
    section = registry.get(TUTOR).body.split(SWITCH_SECTION, 1)[1].split("\n## ", 1)[0]
    for phase, persona in PERSONA_BY_STATE.items():
        assert f"`{persona}`" in section, f"{phase} 推荐的 {persona} 未在手册切换说明中出现"
