"""技能目录扫描与 SKILL.md 解析。

只依赖标准库 + PyYAML(langchain 已传递依赖, 无需新增包)。
解析遵循「宽容跳过、绝不抛到启动链路」: 单个技能损坏只记录 error 并跳过。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from app.skills.schema import SkillManifest
from app.skills.security import list_skill_files

logger = logging.getLogger("skills.loader")

SKILL_FILE_NAME = "SKILL.md"

# \A--- ... --- 之后全部视为正文; 兼容 CRLF 与结尾无换行
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)(.*)\Z", re.DOTALL)


class SkillLoadError(ValueError):
    """单个技能加载失败(目录名不合法 / frontmatter 缺失或非法 / 正文为空)。"""


@dataclass(slots=True)
class SkillScanResult:
    """一次目录扫描的结果: 成功加载的技能 + 可读的失败原因。"""

    skills: dict[str, SkillManifest] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def names(self) -> list[str]:
        return sorted(self.skills)


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """切出 YAML frontmatter 与正文; 缺失或非法 YAML 抛 :class:`SkillLoadError`。"""
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        raise SkillLoadError(f"{SKILL_FILE_NAME} 缺少合法的 YAML frontmatter(--- 包裹)")
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise SkillLoadError(f"frontmatter 不是合法 YAML: {exc}") from exc
    if not isinstance(meta, dict):
        raise SkillLoadError("frontmatter 必须是键值映射")
    return meta, match.group(2)


def load_skill_dir(
    path: Path,
    *,
    known_tools: set[str] | None = None,
    default_max_body_chars: int = 8000,
    max_files: int = 100,
) -> SkillManifest:
    """加载单个技能目录。

    - 目录名必须与 frontmatter ``name`` 一致(避免「目录叫 A、技能叫 B」的越权错觉);
    - ``tools`` 中出现的未知工具名视为配置错误(拼写错误会让模型调用不存在的工具)。
    """
    if not path.is_dir():
        raise SkillLoadError(f"技能路径不是目录: {path}")
    skill_file = path / SKILL_FILE_NAME
    if not skill_file.is_file():
        raise SkillLoadError(f"缺少 {SKILL_FILE_NAME}")

    meta, body = split_frontmatter(skill_file.read_text(encoding="utf-8"))
    if not body.strip():
        raise SkillLoadError("技能正文为空")

    name = str(meta.get("name") or "").strip()
    if name != path.name:
        raise SkillLoadError(f"frontmatter name={name!r} 与目录名 {path.name!r} 不一致")

    if known_tools is not None:
        declared = meta.get("tools") or []
        declared_names = [declared] if isinstance(declared, str) else list(declared)
        unknown = sorted({str(item) for item in declared_names} - known_tools)
        if unknown:
            raise SkillLoadError(f"声明了未知工具: {unknown}(可用: {sorted(known_tools)})")

    meta.setdefault("max_body_chars", default_max_body_chars)
    try:
        manifest = SkillManifest(
            root=path.resolve(),
            body=body,
            files=list_skill_files(path, limit=max_files),
            **meta,
        )
    except (ValidationError, TypeError) as exc:
        raise SkillLoadError(f"frontmatter 校验失败: {exc}") from exc
    return manifest


def scan_skills(
    root: Path,
    *,
    known_tools: set[str] | None = None,
    default_max_body_chars: int = 8000,
    max_files: int = 100,
) -> SkillScanResult:
    """扫描技能根目录(不存在时返回空结果, 不报错)。"""
    result = SkillScanResult()
    if not root.is_dir():
        logger.info("技能目录不存在, 跳过技能加载: %s", root)
        return result

    for entry in sorted(p for p in root.iterdir() if p.is_dir()):
        if entry.name.startswith((".", "_")):
            continue
        try:
            manifest = load_skill_dir(
                entry,
                known_tools=known_tools,
                default_max_body_chars=default_max_body_chars,
                max_files=max_files,
            )
        except SkillLoadError as exc:
            message = f"{entry.name}: {exc}"
            result.errors.append(message)
            logger.warning("技能加载失败, 已跳过 — %s", message)
            continue
        result.skills[manifest.name] = manifest

    logger.info("技能加载完成: %d 个可用, %d 个失败", len(result.skills), len(result.errors))
    return result
