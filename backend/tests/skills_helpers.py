"""技能测试的共用构造函数(非测试模块, pytest 不收集)。

被 tests/test_skills.py 与 tests/test_skills_agent.py 复用: 在 tmp_path 下生成
结构合法的技能目录, 避免每个用例重复拼 frontmatter。
"""

from __future__ import annotations

from pathlib import Path

KNOWN_TOOLS = {"get_current_time", "calculate", "get_weather"}


def write_skill(
    root: Path,
    name: str,
    *,
    description: str = "演示技能。",
    visibility: str = "public",
    requires_permissions: list[str] | None = None,
    tools: list[str] | None = None,
    body: str = "# 步骤\n1. 做事\n",
    extra_files: dict[str, str] | None = None,
) -> Path:
    """在 root 下生成一个技能目录(SKILL.md + 可选附件), 返回其路径。"""
    skill_dir = root / name
    (skill_dir / "references").mkdir(parents=True, exist_ok=True)
    perms = requires_permissions or []
    tool_list = tools or []
    (skill_dir / "SKILL.md").write_text(
        f"""---
name: {name}
description: {description}
version: 1.2.0
visibility: {visibility}
requires_permissions: [{", ".join(perms)}]
tools: [{", ".join(tool_list)}]
---

{body}""",
        encoding="utf-8",
    )
    for rel, content in (extra_files or {}).items():
        target = skill_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return skill_dir
