"""教学法技能(feynman-tutor / feynman-listener / first-principles-drill / boundary-challenger)。

这四个技能是**教学法的载体**(pedagogy-core §5: persona 一律以 Skill 形式落地, 不硬编码
提示词), 所以它们的测试重点与 test_skills.py 不同 —— 那里测的是加载机制, 这里测的是
**技能内容与内核的一致性**:

- 加载/可见性/门禁三条(roadmap P0 #5 的验收项);
- 技能里写的预算数字必须等于 ``PedagogyConfig`` 的默认值(否则教练会向学习者承诺一个
  内核根本不执行的阈值 —— 这类漂移不会被任何机制自动发现, 只能靠对账测试);
- 技能推荐的输出格式必须能通过 ``no_direct_answer`` 护栏(如果 SOP 教模型写「1. 2. 3.」,
  那它教的就是一个必然被截断的写法);
- 只用内核认得的词表(七态 + 四类卡点), 不自创类型 —— 自创类型会直接拉低判分 κ。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest
from app.pedagogy.policy import PedagogyConfig, enforce_no_direct_answer
from app.pedagogy.schemas import GapType
from app.pedagogy.states import FeynmanState
from app.skills import SkillRegistry, scan_skills
from app.tools import ALL_TOOLS

BACKEND_DIR = Path(__file__).resolve().parents[1]
SKILLS_DIR = BACKEND_DIR / "skills"

PEDAGOGY_SKILLS = (
    "feynman-tutor",
    "feynman-listener",
    "first-principles-drill",
    "boundary-challenger",
)

#: 教练技能里那份「配置默认值」表的行格式: | `PEDAGOGY_XXX` | 值 | 含义 |
_CONFIG_ROW = re.compile(r"^\|\s*`(PEDAGOGY_[A-Z0-9_]+)`\s*\|\s*([0-9.]+)\s*\|", re.MULTILINE)

#: SKILL.md 正文里提到的附件路径(read_skill_file 的 path=...)
_REFERENCED_FILE = re.compile(r'path="([^"]+)"')


def _gap_types() -> tuple[str, ...]:
    """内核认得的卡点词表(``GapType`` 是 Literal, 取 args 即枚举)。"""
    return get_args(GapType)


@pytest.fixture(scope="module")
def registry() -> SkillRegistry:
    """按真实工具名校验的注册表(与生产 ``get_skill_registry`` 同一套 known_tools)。"""
    reg = SkillRegistry(SKILLS_DIR, known_tools={tool.name for tool in ALL_TOOLS})
    reg.reload()
    return reg


# --------------------------------------------------------------------------- 加载


def test_all_four_pedagogy_skills_load_with_real_tool_names(registry: SkillRegistry):
    """四个技能必须能被真实工具名校验通过, 且与既有技能共存(不能把示例技能挤掉)。"""
    result = scan_skills(SKILLS_DIR, known_tools={tool.name for tool in ALL_TOOLS})
    assert result.errors == []
    assert set(PEDAGOGY_SKILLS) <= set(result.skills)
    assert {"trip-planner", "expense-report"} <= set(result.skills)
    assert registry.errors == []


def test_frontmatter_name_matches_directory(registry: SkillRegistry):
    """目录名 == frontmatter name(loader 强制), 这里再钉一次以防技能被改名后目录漏改。"""
    for name in PEDAGOGY_SKILLS:
        skill = registry.get(name)
        assert skill is not None, f"技能未加载: {name}"
        assert skill.name == name == skill.root.name
        assert (SKILLS_DIR / name / "SKILL.md").is_file()


def test_declared_tools_are_subset_of_all_tools(registry: SkillRegistry):
    """声明的工具必须真实存在, 否则模型会去调一个不存在的工具。"""
    known = {tool.name for tool in ALL_TOOLS}
    for name in PEDAGOGY_SKILLS:
        skill = registry.get(name)
        assert skill is not None
        assert set(skill.tools) <= known, f"{name} 声明了未知工具"


def test_listener_deliberately_declares_no_tools(registry: SkillRegistry):
    """小白同学**刻意不配工具**: 会用计算器的人不是外行。

    这条不是洁癖 —— 它保证 ``feynman-listener`` 无法自己核对数值, 因此数值验证一定会
    回到教练(有 ``calculate``)手里, 角色边界与工具边界重合。
    """
    listener = registry.get("feynman-listener")
    assert listener is not None
    assert listener.tools == ()
    assert "不要心算" in listener.body and "不会调用任何工具" not in listener.body

    # 另外三个 persona 都要做定量核对(验落点 / 造反例情境 / 核对学习者的数值断言)
    for name in ("feynman-tutor", "first-principles-drill", "boundary-challenger"):
        skill = registry.get(name)
        assert skill is not None
        assert "calculate" in skill.tools, f"{name} 需要 calculate 来避免心算污染证据"


# --------------------------------------------------------------------------- 可见性


def test_pedagogy_skills_require_authentication(registry: SkillRegistry):
    """教学技能对匿名不可见: 循环要写 learner_explanation / mastery_ledger, 需要账号。

    匿名模式下展示教练 = 承诺一件做不到的事(证据无处落库), 所以可见性定为 auth。
    """
    visible_anonymous = {skill.name for skill in registry.visible_to(authenticated=False)}
    assert visible_anonymous.isdisjoint(PEDAGOGY_SKILLS), visible_anonymous

    visible_authed = {
        skill.name for skill in registry.visible_to(permissions=set(), authenticated=True)
    }
    assert set(PEDAGOGY_SKILLS) <= visible_authed


def test_pedagogy_skills_are_auth_visibility_without_permission_gate(registry: SkillRegistry):
    """auth 而非 permission: 教学法是**默认行为**, 不该按权限点开箱。

    若哪天改成 permission, 权限点必须先落进 RBAC 种子(见
    ``test_migrations.test_gated_bundled_skills_require_seeded_permissions``), 否则除 admin
    外永远无人可用。这条测试就是那个改动的绊网。
    """
    for name in PEDAGOGY_SKILLS:
        skill = registry.get(name)
        assert skill is not None
        assert skill.visibility == "auth"
        assert skill.requires_permissions == ()
        assert skill.allows(permissions=frozenset(), authenticated=True)
        assert not skill.allows(permissions=frozenset(), authenticated=False)
        assert skill.allows(permissions=frozenset({"*"}), authenticated=True)


def test_authorize_denies_anonymous_for_every_pedagogy_skill(registry: SkillRegistry):
    """服务端二次鉴权: 匿名直接 ``load_skill`` 也拿不到内容(工具层据此拒绝)。"""
    for name in PEDAGOGY_SKILLS:
        assert registry.authorize(name, permissions=set(), authenticated=False) is None
        assert registry.authorize(name, permissions=set(), authenticated=True) is not None


# --------------------------------------------------------------------------- 渐进披露预算


def test_catalog_fits_budget_without_truncation(registry: SkillRegistry):
    """六个技能全进 L1 目录且不被截断 —— 教练被截掉就等于教学法不存在。

    ``render_catalog`` 超预算时按名称排序截断并追加「另有 N 个技能未展示」, 所以新增技能
    必须同时关注描述长度, 不能只顾写正文。
    """
    visible = registry.visible_to(permissions=frozenset(), authenticated=True)
    catalog = registry.render_catalog(visible)
    assert "另有" not in catalog, f"目录被截断: {catalog}"
    for name in PEDAGOGY_SKILLS:
        assert f"- {name} " in catalog
    assert len(catalog) <= registry.max_catalog_chars


def test_bodies_and_attachments_fit_their_budgets(registry: SkillRegistry):
    """正文与附件都不能被 ``clamp`` 截断: SOP 被切掉尾巴, 模型就照着半份手册执行。"""
    for name in PEDAGOGY_SKILLS:
        skill = registry.get(name)
        assert skill is not None
        assert len(skill.body) <= registry.default_max_body_chars, f"{name} 正文超长"
        rendered = registry.render_body(skill)
        assert "…[内容已截断]" not in rendered, f"{name} 正文被截断"
        assert skill.files, f"{name} 缺少 references 附件(roadmap P0 #5 要求)"
        for rel in skill.files:
            content = registry.read_file(skill, rel)
            assert "…[内容已截断]" not in content, f"{name}/{rel} 附件被截断"


def test_referenced_attachment_paths_exist(registry: SkillRegistry):
    """正文里让模型 ``read_skill_file(path=...)`` 的路径必须真实存在。

    写错一个路径不会报错, 只会让模型收到「文件不存在」然后自行编造 SOP —— 这是最安静的
    一类故障, 只能靠对账。
    """
    for name in PEDAGOGY_SKILLS:
        skill = registry.get(name)
        assert skill is not None
        referenced = set(_REFERENCED_FILE.findall(skill.body))
        assert referenced, f"{name} 正文没有引导读取任何附件(L3 白建了)"
        missing = referenced - set(skill.files)
        assert not missing, f"{name} 引用了不存在的附件: {sorted(missing)}"
        for rel in sorted(referenced):
            # 走真实的 L3 读取路径(含路径穿越防护), 而不只是比字符串;
            # 注意 registry.read_file 返回原文, "[name/path]" 前缀是工具层加的
            assert registry.read_file(skill, rel).lstrip().startswith("#")


# --------------------------------------------------------------------------- 内容与内核一致性


def test_loop_sop_config_defaults_match_pedagogy_config(registry: SkillRegistry):
    """教练手册里的预算数字 == ``PedagogyConfig`` 默认值(逐项对账)。

    这是本次提交里最要紧的一条测试: 手册上的数字是**对学习者的承诺**。配置改了而手册没改,
    教练就会说「不少于 80 字」而内核按 120 字判不达标 —— 学习者被判定失败却不知道为什么,
    而且没有任何运行时报错。
    """
    skill = registry.get("feynman-tutor")
    assert skill is not None
    sop = registry.read_file(skill, "references/loop-sop.md")

    rows = dict(_CONFIG_ROW.findall(sop))
    assert rows, "loop-sop.md 里的配置默认值表被改坏了(解析不到任何行)"

    fields = PedagogyConfig.model_fields
    for env_name, raw in rows.items():
        # PEDAGOGY_MIN_EXPLAIN_CHARS -> Settings.pedagogy_min_explain_chars
        #                            -> PedagogyConfig.min_explain_chars
        field_name = env_name.removeprefix("PEDAGOGY_").lower()
        assert field_name in fields, f"手册写了不存在的配置项: {env_name}"
        expected = fields[field_name].default
        documented = float(raw)
        assert documented == pytest.approx(float(expected)), (
            f"{env_name}: 手册写 {documented}, 内核默认 {expected}"
        )

    # 反向: 除 enabled 之外的阈值都要写进手册(少写一项 = 教练不知道该预算存在)
    documented_fields = {name.removeprefix("PEDAGOGY_").lower() for name in rows}
    missing = {name for name in fields if name != "enabled" and name not in documented_fields}
    assert not missing, f"loop-sop.md 缺少这些配置项的说明: {sorted(missing)}"

    # 手册写的配置项也必须真的是 Settings 上的字段(否则 PEDAGOGY_* 根本不生效)
    from app.config import Settings

    settings_fields = set(Settings.model_fields)
    for env_name in rows:
        assert env_name.lower() in settings_fields, f"Settings 没有 {env_name.lower()}"


def test_tutor_covers_every_feynman_state(registry: SkillRegistry):
    """教练手册必须覆盖七态: 少一态, 模型在那个状态下就没有 SOP, 只能即兴发挥。"""
    skill = registry.get("feynman-tutor")
    assert skill is not None
    body = skill.body
    for state in FeynmanState:
        assert f"`{state.value}`" in body, f"SKILL.md 没有覆盖状态 {state.value}"

    sop = registry.read_file(skill, "references/loop-sop.md")
    for state in FeynmanState:
        assert state.value in sop, f"loop-sop.md 没有 {state.value} 的 SOP"


def test_gap_vocabulary_is_exactly_the_four_types(registry: SkillRegistry):
    """只允许内核认得的四类卡点: 自创类型会直接拉低判分一致率(Cohen's κ)。"""
    allowed = set(_gap_types())
    assert allowed == {"jargon_shield", "causal_gap", "broken_analogy", "no_boundary"}

    # 手册里出现的 `xxx_gap` / `xxx_shield` / `xxx_analogy` / no_boundary 风格 token
    token = re.compile(r"`([a-z][a-z0-9_]*(?:_gap|_shield|_analogy|_boundary))`")
    for name in PEDAGOGY_SKILLS:
        skill = registry.get(name)
        assert skill is not None
        texts = [skill.body] + [registry.read_file(skill, rel) for rel in skill.files]
        for text in texts:
            found = set(token.findall(text))
            unknown = found - allowed
            assert not unknown, f"{name} 用了内核不认的卡点类型: {sorted(unknown)}"


def test_guard_names_match_the_kernel(registry: SkillRegistry):
    """手册提到的护栏名必须与 ``Guard`` 枚举一致(否则日志/看板与文档对不上)。"""
    from app.pedagogy.policy import Guard

    skill = registry.get("feynman-tutor")
    assert skill is not None
    sop = registry.read_file(skill, "references/loop-sop.md")
    for guard in Guard:
        assert guard.value in skill.body or guard.value in sop, f"手册未提及护栏 {guard.value}"

    # 手册不得出现内核没有的护栏名( invent 一个护栏 = 教练会向学习者承诺不存在的保护)
    known = {guard.value for guard in Guard}
    mentioned = set(re.findall(r"`([a-z][a-z0-9_]*(?:_cap|_budget|_answer|_ratio|_hatch))`", sop))
    assert mentioned <= known, f"手册写了内核没有的护栏: {sorted(mentioned - known)}"


def test_sop_output_templates_survive_no_direct_answer(registry: SkillRegistry):
    """手册教的 ``Detect``/``Return`` 输出模板必须**不被护栏截断**。

    这条测试把「文档」和「内核实现」焊在一起: ``no_direct_answer`` 的触发信号是
    ≥3 个行首编号步骤, 或「答案：/结果是：/最终答案」。SOP 明确要求在这两个状态下
    用「缺的是 → 该问的是」短结构而非编号列表 —— 这里用模板的实际填充结果验证它真的安全,
    并用一个「错误写法」反证这条测试有牙齿。
    """
    config = PedagogyConfig()
    skill = registry.get("feynman-tutor")
    assert skill is not None
    sop = registry.read_file(skill, "references/loop-sop.md")

    def _template(section: str) -> str:
        block = sop.split(section, 1)[1]
        return block.split("```", 2)[1]

    detect = _template("## 3. `Detect`")
    ret = _template("## 4. `Return`")
    assert detect.strip() and ret.strip()

    filled_detect = detect.replace("<他的原话片段>", "点积越大越相关").replace(
        "<理由>", "点积衡量方向一致性"
    )
    filled_return = ret.replace("<Atom 命题>", "维度越高点积方差越大").replace(
        "<标题>", "Attention Is All You Need"
    )

    for state in (FeynmanState.DETECT, FeynmanState.RETURN):
        for text in (filled_detect, filled_return):
            kept, violation = enforce_no_direct_answer(state, text, config)
            assert violation is None, f"{state} 下模板被护栏截断: {violation}"
            assert kept == text

    # 反证: 手册明令禁止的编号列表写法确实会被截断(说明上面通过不是护栏失效)
    forbidden = "我们一步步来：\n1. 先算点积\n2. 再除以根号 d_k\n3. 最后 softmax"
    _, violation = enforce_no_direct_answer(FeynmanState.DETECT, forbidden, config)
    assert violation is not None and violation.guard == "no_direct_answer"


def test_escape_hatch_requires_confirmation_before_direct_answer(registry: SkillRegistry):
    """逃生舱: 手册必须写「先确认再直答」且「不记掌握度」, 与 ``escape_hatch`` 护栏同口径。"""
    skill = registry.get("feynman-tutor")
    assert skill is not None
    sop = registry.read_file(skill, "references/loop-sop.md")
    escaped = sop.split("## 7. `Escaped`", 1)[1]
    assert "先确认" in escaped
    assert "不记" in escaped or "不会记入掌握度" in escaped
    assert "补测" in escaped


def test_personas_declare_their_handoff_boundaries(registry: SkillRegistry):
    """四个 persona 必须互相指认边界, 否则会出现「三个人同时抢话筒」。"""
    expectations = {
        "feynman-tutor": ("feynman-listener", "first-principles-drill", "boundary-challenger"),
        "feynman-listener": ("feynman-tutor",),
        "first-principles-drill": ("boundary-challenger", "feynman-listener", "feynman-tutor"),
        "boundary-challenger": ("first-principles-drill", "feynman-listener", "feynman-tutor"),
    }
    for name, others in expectations.items():
        skill = registry.get(name)
        assert skill is not None
        for other in others:
            assert other in skill.body, f"{name} 没有交代与 {other} 的边界"


def test_sub_personas_never_claim_to_decide_closure(registry: SkillRegistry):
    """闭合裁决只属于内核: 三个辅助 persona 都不得自行宣布通过。

    persona 一旦能说「你通过了」, 掌握度账本就会出现没有证据支撑的 closed 记录 ——
    这正是 pedagogy-core 设计信条 1「教学法是控制流, 不是提示词」要防的事。
    """
    for name in ("feynman-listener", "first-principles-drill", "boundary-challenger"):
        skill = registry.get(name)
        assert skill is not None
        assert "不要自己宣布" in skill.body or "不宣布闭合" in skill.body or "不宣布" in skill.body
