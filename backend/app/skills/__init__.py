"""Agent 技能(skills)子系统。

技能 = 磁盘上的一个目录(``SKILL.md`` + 可选附件), 用「渐进披露」把领域知识/操作流程
按需喂给模型, 避免把所有能力都塞进常驻系统提示或工具 schema:

- L1 目录(name + description) → 由 :class:`~app.middleware.agent_skills.SkillMiddleware` 注入系统提示;
- L2 正文 → ``load_skill`` 工具;
- L3 附件 → ``read_skill_file`` 工具。

权限复用既有 RBAC(``AuthContext.permissions``), 由技能 frontmatter 的
``visibility`` / ``requires_permissions`` 声明。
"""

from app.skills.loader import SkillLoadError, SkillScanResult, load_skill_dir, scan_skills
from app.skills.registry import SkillRegistry, get_skill_registry
from app.skills.schema import SkillManifest
from app.skills.security import SkillSecurityError

__all__ = [
    "SkillLoadError",
    "SkillManifest",
    "SkillRegistry",
    "SkillScanResult",
    "SkillSecurityError",
    "get_skill_registry",
    "load_skill_dir",
    "scan_skills",
]
