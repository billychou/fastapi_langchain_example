"""技能加载/注册表单元测试: frontmatter 解析、权限过滤、三级渲染、路径安全、热重扫。

全部用 tmp_path 构造技能目录, 不依赖仓库内置技能与外部服务。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from app.skills import SkillRegistry, SkillSecurityError, load_skill_dir, scan_skills
from app.skills.loader import SkillLoadError, split_frontmatter
from app.skills.schema import SkillManifest

from skills_helpers import KNOWN_TOOLS, write_skill


def make_registry(root: Path, **kwargs) -> SkillRegistry:
    registry = SkillRegistry(root, known_tools=KNOWN_TOOLS, **kwargs)
    registry.reload()
    return registry


# --------------------------------------------------------------------------- frontmatter
def test_split_frontmatter_parses_meta_and_body():
    meta, body = split_frontmatter("---\nname: a\ntools: [x, y]\n---\n正文内容\n")
    assert meta == {"name": "a", "tools": ["x", "y"]}
    assert body.strip() == "正文内容"


def test_split_frontmatter_rejects_missing_block():
    with pytest.raises(SkillLoadError, match="frontmatter"):
        split_frontmatter("# 只有正文\n")


def test_split_frontmatter_rejects_invalid_yaml():
    with pytest.raises(SkillLoadError, match="YAML"):
        split_frontmatter("---\nname: [unclosed\n---\nbody\n")


def test_load_skill_dir_normalizes_description_whitespace(tmp_path):
    write_skill(tmp_path, "demo", description="第一行\n  第二行   多余空格")
    skill = load_skill_dir(tmp_path / "demo", known_tools=KNOWN_TOOLS)
    assert skill.description == "第一行 第二行 多余空格"


def test_load_skill_dir_rejects_name_mismatch(tmp_path):
    write_skill(tmp_path, "demo")
    (tmp_path / "demo" / "SKILL.md").write_text(
        (tmp_path / "demo" / "SKILL.md").read_text(encoding="utf-8").replace(
            "name: demo", "name: other"
        ),
        encoding="utf-8",
    )
    with pytest.raises(SkillLoadError, match="不一致"):
        load_skill_dir(tmp_path / "demo", known_tools=KNOWN_TOOLS)


def test_load_skill_dir_rejects_empty_body(tmp_path):
    write_skill(tmp_path, "demo", body="   \n")
    with pytest.raises(SkillLoadError, match="正文为空"):
        load_skill_dir(tmp_path / "demo", known_tools=KNOWN_TOOLS)


def test_load_skill_dir_rejects_unknown_tool(tmp_path):
    write_skill(tmp_path, "demo", tools=["get_current_time", "typo_tool"])
    with pytest.raises(SkillLoadError, match="未知工具"):
        load_skill_dir(tmp_path / "demo", known_tools=KNOWN_TOOLS)


def test_load_skill_dir_requires_permissions_for_gated_visibility(tmp_path):
    write_skill(tmp_path, "demo", visibility="permission")
    with pytest.raises(SkillLoadError, match="requires_permissions"):
        load_skill_dir(tmp_path / "demo", known_tools=KNOWN_TOOLS)


def test_load_skill_dir_lists_attachment_files_without_skill_md(tmp_path):
    write_skill(tmp_path, "demo", extra_files={"references/a.md": "A", "assets/b.txt": "B"})
    skill = load_skill_dir(tmp_path / "demo", known_tools=KNOWN_TOOLS)
    assert set(skill.files) == {"references/a.md", "assets/b.txt"}


# --------------------------------------------------------------------------- 扫描容错
def test_scan_skips_broken_skill_and_reports_error(tmp_path):
    write_skill(tmp_path, "good")
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "SKILL.md").write_text("没有 frontmatter\n", encoding="utf-8")
    (tmp_path / "_private").mkdir()  # 下划线开头目录视为私有, 直接忽略

    result = scan_skills(tmp_path, known_tools=KNOWN_TOOLS)
    assert result.names == ["good"]
    assert len(result.errors) == 1
    assert result.errors[0].startswith("broken:")


def test_scan_missing_root_returns_empty(tmp_path):
    result = scan_skills(tmp_path / "does-not-exist")
    assert result.skills == {}
    assert result.errors == []


# --------------------------------------------------------------------------- 权限过滤
def test_visibility_filtering_matrix(tmp_path):
    write_skill(tmp_path, "pub", visibility="public")
    write_skill(tmp_path, "auth-only", visibility="auth")
    write_skill(
        tmp_path,
        "gated",
        visibility="permission",
        requires_permissions=["skill:gated:use"],
    )
    registry = make_registry(tmp_path)

    def names(**kwargs) -> list[str]:
        return [s.name for s in registry.visible_to(**kwargs)]

    assert names(permissions=set(), authenticated=False) == ["pub"]
    assert names(permissions=set(), authenticated=True) == ["auth-only", "pub"]
    assert names(permissions={"skill:gated:use"}, authenticated=True) == [
        "auth-only",
        "gated",
        "pub",
    ]
    # RBAC 通配权限(*, admin 角色)可见全部技能
    assert names(permissions={"*"}, authenticated=True) == ["auth-only", "gated", "pub"]


def test_authorize_returns_none_for_invisible_skill(tmp_path):
    write_skill(tmp_path, "gated", visibility="permission", requires_permissions=["skill:gated:use"])
    registry = make_registry(tmp_path)
    assert registry.authorize("gated", permissions=set(), authenticated=True) is None
    assert registry.authorize("gated", permissions={"skill:gated:use"}, authenticated=True)
    assert registry.authorize("nope", permissions={"*"}, authenticated=True) is None


def test_disabled_registry_hides_everything(tmp_path):
    write_skill(tmp_path, "pub")
    registry = SkillRegistry(tmp_path, enabled=False, known_tools=KNOWN_TOOLS)
    registry.reload()
    assert registry.all() == []
    assert registry.visible_to(permissions={"*"}, authenticated=True) == []
    assert registry.render_catalog([]) == ""


# --------------------------------------------------------------------------- L1 目录
def test_render_catalog_contains_names_and_header(tmp_path):
    write_skill(tmp_path, "alpha", description="做 A。")
    write_skill(tmp_path, "beta", description="做 B。")
    registry = make_registry(tmp_path)
    catalog = registry.render_catalog(registry.all())
    assert "load_skill" in catalog
    assert "- alpha (v1.2.0): 做 A。" in catalog
    assert "- beta (v1.2.0): 做 B。" in catalog


def test_render_catalog_truncates_and_reports_hidden_count(tmp_path):
    for i in range(12):
        write_skill(tmp_path, f"skill-{i:02d}", description=f"描述 {i} " + "长" * 40)
    registry = make_registry(tmp_path, max_catalog_chars=300)
    skills = registry.all()
    catalog = registry.render_catalog(skills)
    assert len(catalog) <= 400
    assert len(skills) == 12
    assert "另有" in catalog and "list_skills" in catalog
    assert catalog.count("\n- skill-") < 12


def test_render_catalog_always_shows_at_least_one_skill(tmp_path):
    write_skill(tmp_path, "only", description="很长的描述" * 20)
    registry = make_registry(tmp_path)
    catalog = registry.render_catalog(registry.all(), max_chars=40)
    assert "- only:" in catalog


# --------------------------------------------------------------------------- L2 正文
def test_render_body_wraps_untrusted_and_hints_tools(tmp_path):
    write_skill(tmp_path, "demo", tools=["calculate"], extra_files={"references/a.md": "A"})
    registry = make_registry(tmp_path)
    body = registry.render_body(registry.get("demo"))
    assert body.startswith('<skill name="demo"')
    assert "而非新的系统指令" in body
    assert "calculate" in body
    assert "references/a.md" in body


def test_render_body_clamps_to_configured_limit(tmp_path):
    write_skill(tmp_path, "demo", body="字" * 5000)
    registry = make_registry(tmp_path, default_max_body_chars=500)
    body = registry.render_body(registry.get("demo"))
    assert "内容已截断" in body
    assert body.count("字") == 500


def test_skill_level_max_body_chars_cannot_exceed_global_limit(tmp_path):
    skill_dir = write_skill(tmp_path, "demo", body="字" * 5000)
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(text.replace("tools: []", "tools: []\nmax_body_chars: 4000"), encoding="utf-8")
    registry = make_registry(tmp_path, default_max_body_chars=600)
    body = registry.render_body(registry.get("demo"))
    assert body.count("字") == 600


# --------------------------------------------------------------------------- L3 附件
def test_read_file_returns_attachment_content(tmp_path):
    write_skill(tmp_path, "demo", extra_files={"references/a.md": "清单内容"})
    registry = make_registry(tmp_path)
    assert registry.read_file(registry.get("demo"), "references/a.md") == "清单内容"


@pytest.mark.parametrize("rel_path", ["../secret.md", "/etc/passwd", "references/../../x.md", "", "references"])
def test_read_file_rejects_traversal_and_non_files(tmp_path, rel_path):
    write_skill(tmp_path, "demo", extra_files={"references/a.md": "A"})
    (tmp_path / "secret.md").write_text("顶层机密", encoding="utf-8")
    registry = make_registry(tmp_path)
    with pytest.raises(SkillSecurityError):
        registry.read_file(registry.get("demo"), rel_path)


def test_read_file_rejects_symlink_escape(tmp_path):
    skill_dir = write_skill(tmp_path, "demo", extra_files={"references/a.md": "A"})
    outside = tmp_path / "outside.md"
    outside.write_text("外部文件", encoding="utf-8")
    os.symlink(outside, skill_dir / "references" / "link.md")
    registry = make_registry(tmp_path)
    with pytest.raises(SkillSecurityError):
        registry.read_file(registry.get("demo"), "references/link.md")


def test_read_file_rejects_oversized_file(tmp_path):
    write_skill(tmp_path, "demo", extra_files={"references/big.md": "x" * 4096})
    registry = make_registry(tmp_path, max_file_bytes=1024)
    with pytest.raises(SkillSecurityError, match="文件过大"):
        registry.read_file(registry.get("demo"), "references/big.md")


def test_read_file_rejects_non_utf8_file(tmp_path):
    write_skill(tmp_path, "demo", extra_files={"references/a.md": "A"})
    (tmp_path / "demo" / "references" / "raw.bin").write_bytes(b"\xff\xfe\x00\x01")
    registry = make_registry(tmp_path)
    with pytest.raises(SkillSecurityError, match="UTF-8"):
        registry.read_file(registry.get("demo"), "references/raw.bin")


# --------------------------------------------------------------------------- 热重扫
def test_registry_picks_up_new_skill_without_reload_call(tmp_path):
    write_skill(tmp_path, "first")
    registry = make_registry(tmp_path)
    assert [s.name for s in registry.all()] == ["first"]

    write_skill(tmp_path, "second")
    assert [s.name for s in registry.all()] == ["first", "second"]


def test_registry_picks_up_edited_skill_body(tmp_path):
    skill_dir = write_skill(tmp_path, "demo", body="旧正文")
    registry = make_registry(tmp_path)
    assert "旧正文" in registry.get("demo").body

    os.utime(skill_dir / "SKILL.md", ns=(1, 1))  # 先制造确定的 mtime 差异
    (skill_dir / "SKILL.md").write_text(
        (skill_dir / "SKILL.md").read_text(encoding="utf-8").replace("旧正文", "新正文"),
        encoding="utf-8",
    )
    assert "新正文" in registry.get("demo").body


def test_registry_exposes_load_errors(tmp_path):
    write_skill(tmp_path, "good")
    (tmp_path / "bad").mkdir()
    registry = SkillRegistry(tmp_path, known_tools=KNOWN_TOOLS)
    registry.reload()
    assert registry.errors and registry.errors[0].startswith("bad:")


# --------------------------------------------------------------------------- 仓库内置技能
def test_bundled_skills_load_with_real_tool_names():
    """仓库自带技能必须能被真实工具名校验通过(防止示例与 tools.py 脱节)。"""
    from app.tools import ALL_TOOLS

    root = Path(__file__).resolve().parents[1] / "skills"
    result = scan_skills(root, known_tools={tool.name for tool in ALL_TOOLS})
    assert result.errors == []
    assert {"trip-planner", "expense-report"} <= set(result.skills)

    trip = result.skills["trip-planner"]
    assert trip.visibility == "public"
    assert trip.allows(permissions=set(), authenticated=False)

    expense = result.skills["expense-report"]
    assert expense.visibility == "permission"
    assert not expense.allows(permissions=set(), authenticated=True)
    assert expense.allows(permissions={"skill:expense-report:use"}, authenticated=True)
    assert expense.files == ("references/policy.md",)


def test_manifest_is_immutable(tmp_path):
    write_skill(tmp_path, "demo")
    skill = load_skill_dir(tmp_path / "demo", known_tools=KNOWN_TOOLS)
    assert isinstance(skill, SkillManifest)
    with pytest.raises(Exception):  # noqa: B017, PT011 - pydantic ValidationError 类型随版本变化
        skill.description = "改写"
