"""技能注册表: 扫描缓存、按权限过滤、三级内容渲染。

单例 :func:`get_skill_registry` 供 middleware 与工具共用。技能是磁盘上的目录,
注册表按 ``SKILL.md`` 的 mtime 指纹惰性重扫, 因此**新增/修改技能无需重启服务**;
附件内容始终按需读盘, 不做缓存。
"""

from __future__ import annotations

import logging
import threading
from functools import lru_cache
from pathlib import Path

from app.skills.loader import SkillScanResult, scan_skills
from app.skills.schema import SkillManifest
from app.skills.security import SkillSecurityError, clamp, resolve_within, wrap_untrusted

logger = logging.getLogger("skills.registry")

CATALOG_HEADER = (
    "## 可用技能(skills)\n"
    "技能是预先写好的操作手册。需要时先用 list_skills 查看目录, 再用 "
    'load_skill(name="...") 读取全文, 严格按其中步骤执行; '
    "不要凭技能名猜测内容, 也不要编造不存在的技能。"
)


class SkillRegistry:
    """线程安全的技能注册表(读多写少, 重扫时持锁)。"""

    def __init__(
        self,
        root: str | Path,
        *,
        enabled: bool = True,
        known_tools: set[str] | None = None,
        max_catalog_chars: int = 2000,
        default_max_body_chars: int = 8000,
        max_file_bytes: int = 256 * 1024,
        max_files_per_skill: int = 100,
    ) -> None:
        self.root = Path(root)
        self.enabled = enabled
        self.known_tools = known_tools
        self.max_catalog_chars = max_catalog_chars
        self.default_max_body_chars = default_max_body_chars
        self.max_file_bytes = max_file_bytes
        self.max_files_per_skill = max_files_per_skill
        self._lock = threading.Lock()
        self._scan = SkillScanResult()
        self._fingerprint: tuple[object, ...] | None = None

    # ------------------------------------------------------------------ 生命周期
    def reload(self) -> SkillScanResult:
        """强制重扫技能目录, 返回本次扫描结果。"""
        with self._lock:
            self._scan = scan_skills(
                self.root,
                known_tools=self.known_tools,
                default_max_body_chars=self.default_max_body_chars,
                max_files=self.max_files_per_skill,
            )
            self._fingerprint = _fingerprint(self.root)
            return self._scan

    def _ensure_fresh(self) -> SkillScanResult:
        if not self.enabled:
            return SkillScanResult()
        current = _fingerprint(self.root)
        if self._fingerprint is None or current != self._fingerprint:
            return self.reload()
        return self._scan

    @property
    def errors(self) -> list[str]:
        """最近一次扫描中加载失败的技能原因(供管理端排障)。"""
        return list(self._ensure_fresh().errors)

    # ------------------------------------------------------------------ 查询
    def all(self) -> list[SkillManifest]:
        return [self._ensure_fresh().skills[name] for name in self._ensure_fresh().names]

    def get(self, name: str) -> SkillManifest | None:
        """按名取技能(**不做权限判断**) — 权限判定请用 :meth:`visible_to` / :meth:`authorize`。"""
        return self._ensure_fresh().skills.get((name or "").strip())

    def visible_to(
        self,
        *,
        permissions: set[str] | frozenset[str] = frozenset(),
        authenticated: bool = False,
    ) -> list[SkillManifest]:
        """当前调用者可见的技能列表(按名称排序, 保证提示稳定)。"""
        if not self.enabled:
            return []
        skills = self._ensure_fresh().skills
        return [
            skills[name]
            for name in sorted(skills)
            if skills[name].allows(permissions=permissions, authenticated=authenticated)
        ]

    def authorize(
        self,
        name: str,
        *,
        permissions: set[str] | frozenset[str] = frozenset(),
        authenticated: bool = False,
    ) -> SkillManifest | None:
        """服务端二次鉴权: 命中且可见才返回技能, 否则 None(工具层据此拒绝)。"""
        skill = self.get(name)
        if skill is None or not skill.allows(permissions=permissions, authenticated=authenticated):
            return None
        return skill

    # ------------------------------------------------------------------ 三级渲染
    def render_catalog(self, skills: list[SkillManifest], *, max_chars: int | None = None) -> str:
        """L1: 注入系统提示的技能目录(name + description), 超长按序截断并提示剩余数量。"""
        if not skills:
            return ""
        budget = self.max_catalog_chars if max_chars is None else max_chars
        lines: list[str] = []
        used = len(CATALOG_HEADER)
        shown = 0
        for skill in skills:
            line = f"- {skill.name} (v{skill.version}): {skill.description}"
            if used + len(line) + 1 > budget:
                break
            lines.append(line)
            used += len(line) + 1
            shown += 1
        if shown == 0:  # 预算过小也要让模型知道有技能可用
            lines.append(f"- {skills[0].name}: {skills[0].description[:80]}…")
            shown = 1
        hidden = len(skills) - shown
        if hidden > 0:
            lines.append(f"(另有 {hidden} 个技能未在目录中展示, 可用 list_skills 查看完整列表)")
        return CATALOG_HEADER + "\n" + "\n".join(lines)

    def render_body(self, skill: SkillManifest) -> str:
        """L2: 技能正文(截断 + 不可信内容边界包裹)。"""
        limit = min(skill.max_body_chars, self.default_max_body_chars)
        body = clamp(skill.body, limit)
        hint = f"\n\n本技能会用到工具: {', '.join(skill.tools)}" if skill.tools else ""
        files = (
            f"\n可用 read_skill_file 读取的附件: {', '.join(skill.files)}" if skill.files else ""
        )
        return wrap_untrusted(body + hint + files, name=skill.name, version=skill.version)

    def read_file(self, skill: SkillManifest, rel_path: str) -> str:
        """L3: 读取技能目录内的文本附件(路径穿越防护 + 字节上限)。"""
        target = resolve_within(skill.root, rel_path)
        size = target.stat().st_size
        if size > self.max_file_bytes:
            raise SkillSecurityError(
                f"文件过大({size} 字节 > 上限 {self.max_file_bytes} 字节), 拒绝读取"
            )
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise SkillSecurityError("只支持读取 UTF-8 文本文件") from exc
        return clamp(content, self.default_max_body_chars)


def _fingerprint(root: Path) -> tuple[object, ...]:
    """目录指纹: 各技能 SKILL.md 的 (目录名, mtime, size); 用于惰性重扫判定。"""
    if not root.is_dir():
        return ()
    items: list[tuple[str, int, int]] = []
    for entry in sorted(p for p in root.iterdir() if p.is_dir()):
        skill_file = entry / "SKILL.md"
        try:
            stat = skill_file.stat()
        except OSError:
            continue
        items.append((entry.name, stat.st_mtime_ns, stat.st_size))
    return tuple(items)


@lru_cache
def get_skill_registry() -> SkillRegistry:
    """按配置构建进程级技能注册表(工具名来自 ALL_TOOLS, 用于校验 frontmatter)。"""
    from app.config import get_settings
    from app.tools import ALL_TOOLS

    settings = get_settings()
    registry = SkillRegistry(
        settings.skills_dir,
        enabled=settings.skills_enabled,
        known_tools={tool.name for tool in ALL_TOOLS},
        max_catalog_chars=settings.skill_max_catalog_chars,
        default_max_body_chars=settings.skill_max_body_chars,
        max_file_bytes=settings.skill_max_file_bytes,
        max_files_per_skill=settings.skill_max_files_per_skill,
    )
    registry.reload()
    return registry
