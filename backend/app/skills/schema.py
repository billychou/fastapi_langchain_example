"""技能清单(SkillManifest)的数据结构与校验规则。

一个技能 = ``skills_dir/<name>/`` 目录, 至少包含带 YAML frontmatter 的 ``SKILL.md``:

    ---
    name: trip-planner
    description: 根据天气与时间安排一日行程。当用户要求「规划行程 / 出去玩 / 安排一天」时使用。
    version: 1.0.0
    visibility: public          # public | auth | permission
    requires_permissions: []    # visibility=permission 时必填
    tools: [get_weather]        # 技能正文会引导模型使用的既有工具名
    max_body_chars: 8000        # 可选, 覆盖全局 SKILL_MAX_BODY_CHARS
    ---
    正文: 分步骤的操作说明(渐进披露的 L2 内容)。

frontmatter 只做声明, 不含可执行逻辑; 校验失败的技能会被跳过并记录告警,
绝不让一个坏目录拖垮服务启动。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# 技能名同时用作目录名与工具参数, 限定为 URL/文件名安全的短横线小写风格
SKILL_NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"

Visibility = Literal["public", "auth", "permission"]


class SkillManifest(BaseModel):
    """解析并校验后的技能元数据 + 正文(不可变)。"""

    model_config = ConfigDict(frozen=True)

    name: str = Field(pattern=SKILL_NAME_PATTERN, max_length=64)
    description: str = Field(min_length=1, max_length=500)
    version: str = Field(default="0.0.0", max_length=32)
    visibility: Visibility = "auth"
    requires_permissions: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    max_body_chars: int = Field(default=8000, ge=200, le=32_000)
    root: Path
    body: str = Field(min_length=1)
    files: tuple[str, ...] = ()

    @field_validator("description", mode="before")
    @classmethod
    def _normalize_description(cls, value: object) -> object:
        # 描述常驻系统提示, 压掉换行与多余空格, 保证目录渲染整齐
        if isinstance(value, str):
            return " ".join(value.split())
        return value

    @field_validator("requires_permissions", "tools", "files", mode="before")
    @classmethod
    def _to_str_tuple(cls, value: object) -> object:
        if value is None or value == "":
            return ()
        if isinstance(value, str):
            return tuple(item.strip() for item in value.split(",") if item.strip())
        if isinstance(value, (list, tuple, set)):
            return tuple(str(item).strip() for item in value if str(item).strip())
        return value

    @field_validator("body", mode="before")
    @classmethod
    def _strip_body(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _check_permission_visibility(self) -> SkillManifest:
        if self.visibility == "permission" and not self.requires_permissions:
            raise ValueError("visibility=permission 时必须声明 requires_permissions")
        return self

    def allows(self, *, permissions: set[str] | frozenset[str], authenticated: bool) -> bool:
        """当前调用者是否可见此技能(权限判定唯一入口)。"""
        if self.visibility == "public":
            return True
        if not authenticated:
            return False
        if "*" in permissions:
            return True
        if self.visibility == "auth":
            return True
        return all(code in permissions for code in self.requires_permissions)
