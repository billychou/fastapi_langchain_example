"""技能(skills)API 的响应模型。

字段与 :class:`~app.skills.schema.SkillManifest` 对齐, 但**刻意不含磁盘路径**
(技能目录位置是部署细节, 不对外暴露), 且 ``requires_permissions`` 只出现在
调用者本就可见的技能上(不可见的技能直接不在列表里)。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Visibility = Literal["public", "auth", "permission"]


class SkillSummary(BaseModel):
    """L1 目录项: 与模型在系统提示里看到的技能目录同源。"""

    name: str = Field(..., description="技能名(目录名, 也是 load_skill 的参数)")
    version: str = Field(..., description="技能版本(frontmatter 声明)")
    description: str = Field(..., description="用途与触发场景")
    visibility: Visibility = Field(
        ..., description="public=匿名可见 / auth=登录可见 / permission=按权限点"
    )
    requires_permissions: list[str] = Field(
        default_factory=list, description="visibility=permission 时需要同时具备的权限编码"
    )
    tools: list[str] = Field(default_factory=list, description="技能正文会引导模型调用的既有工具名")
    files: list[str] = Field(
        default_factory=list, description="可用 read_skill_file / 附件接口读取的相对路径"
    )


class SkillDetail(SkillSummary):
    """L2 详情: 额外给出模型真正会读到的正文。"""

    max_body_chars: int = Field(..., description="该技能正文的字符上限(全局与技能声明取小)")
    body: str = Field(
        ...,
        description="渲染后的正文: 与 load_skill 返回给模型的内容完全一致(已截断并包裹不可信内容边界)",
    )


class SkillFile(BaseModel):
    """L3 附件内容。"""

    name: str = Field(..., description="所属技能名")
    path: str = Field(..., description="技能目录内的相对路径")
    content: str = Field(..., description="UTF-8 文本内容(超长会被截断)")
    chars: int = Field(..., description="返回内容的字符数")


class SkillReloadResult(BaseModel):
    """管理端热重载结果。"""

    count: int = Field(..., description="本次成功加载的技能数")
    names: list[str] = Field(default_factory=list, description="成功加载的技能名(已排序)")
    errors: list[str] = Field(default_factory=list, description="加载失败的技能及原因(排障用)")
