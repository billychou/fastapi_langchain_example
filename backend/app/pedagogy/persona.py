"""卡点 -> persona 技能的推荐映射(pedagogy-core §5「什么时候把话筒交给别的 persona」)。

persona 一律是 ``backend/skills/<name>/`` 里的技能(教学法可版本化、可 A/B), 所以这里
推荐的值是**技能目录名**, 不是提示词片段 —— 模型拿到名字后用 ``load_skill`` 自己读 SOP。

映射刻意与 ``skills/feynman-tutor/SKILL.md`` 的三段「切换」说明逐字对应, 并由
``tests/test_pedagogy_persona.py`` 对账: 手册说「术语盾切小白同学」而内核推荐了别人,
模型就会同时收到两套互相矛盾的编排指令, 而且不会有任何报错。
"""

from __future__ import annotations

from app.pedagogy.schemas import GapType, PedagogyState
from app.pedagogy.states import FeynmanState

#: 教练自己(默认 persona): 组织循环、裁决证据齐备性。
TUTOR = "feynman-tutor"
#: 小白同学: 只听不讲, 专门戳「用术语解释术语」。
LISTENER = "feynman-listener"
#: Why-chain 下探: 把断言一路追到 definition/axiom/observable。
DRILL = "first-principles-drill"
#: 抬杠同学: 索取反例、适用边界、类比失效点。
CHALLENGER = "boundary-challenger"

#: 四类卡点 -> 该由谁来问。四类必须全覆盖(缺一类就退回教练, 相当于没有针对性)。
PERSONA_BY_GAP: dict[GapType, str] = {
    "jargon_shield": LISTENER,
    "causal_gap": DRILL,
    "broken_analogy": CHALLENGER,
    "no_boundary": CHALLENGER,
}

#: 没有卡点时按状态推荐: ``Simplify`` 要的是类比/反例/边界, 天然是抬杠同学的活。
PERSONA_BY_STATE: dict[FeynmanState, str] = {
    FeynmanState.SIMPLIFY: CHALLENGER,
}


def recommend_persona(state: PedagogyState) -> str:
    """这一轮该由哪个 persona 说话。

    优先级: **严重度最高的未闭合卡点** > 当前状态 > 教练兜底。
    卡点优先是因为它是证据驱动的(「他断在哪」比「流程走到哪」更能决定语气);
    同严重度时按内核排序保持稳定, 免得同一份状态在两轮里推荐出不同的人。
    """
    for gap in sorted(state.open_gaps, key=lambda item: (-item.severity, item.type)):
        persona = PERSONA_BY_GAP.get(gap.type)
        if persona:
            return persona
    return PERSONA_BY_STATE.get(state.state, TUTOR)
