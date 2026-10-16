"""智能体注册表的请求/响应模型。

``counts`` 由四个 refs 列的长度算出(不存冗余列), 与前端卡片 chips 一一对应;
``mcp`` / ``rag`` 本期只是占位计数, 真实连接下一阶段接入。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

#: 与技能名同风格: 小写字母数字开头, 允许连字符/下划线, 兼顾 URL 与工具命名安全。
AGENT_KEY_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"

AgentRole = Literal["supervisor", "executor"]
AgentStatus = Literal["active", "paused"]

_MAX_REFS = 64


def _normalize_refs(value: object) -> object:
    """去空白 + 保序去重; 空值统一成空列表。"""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return value
    seen: list[str] = []
    for item in value:
        key = str(item).strip()
        if key and key not in seen:
            seen.append(key)
    return seen


class AgentCounts(BaseModel):
    """卡片底部四个计数 chips。"""

    local: int = 0
    mcp: int = 0
    rag: int = 0
    skills: int = 0


class AgentSummary(BaseModel):
    """列表/卡片视图。"""

    agent_key: str
    name: str
    role: AgentRole
    status: AgentStatus
    description: str
    counts: AgentCounts


class AgentDetail(AgentSummary):
    """详情/编辑视图: 含提示词与全部引用。"""

    system_prompt: str
    tool_refs: list[str]
    skill_refs: list[str]
    mcp_refs: list[str]
    rag_refs: list[str]
    sort_order: int


class _RefsMixin(BaseModel):
    tool_refs: list[str] = Field(default_factory=list, max_length=_MAX_REFS)
    skill_refs: list[str] = Field(default_factory=list, max_length=_MAX_REFS)
    mcp_refs: list[str] = Field(default_factory=list, max_length=_MAX_REFS)
    rag_refs: list[str] = Field(default_factory=list, max_length=_MAX_REFS)

    @field_validator("tool_refs", "skill_refs", "mcp_refs", "rag_refs", mode="before")
    @classmethod
    def _normalize(cls, value: object) -> object:
        return _normalize_refs(value)


class AgentCreate(_RefsMixin):
    agent_key: str = Field(pattern=AGENT_KEY_PATTERN, max_length=64)
    name: str = Field(min_length=1, max_length=64)
    role: AgentRole = "executor"
    description: str = Field(default="", max_length=500)
    system_prompt: str = ""


class AgentUpdate(_RefsMixin):
    """部分更新: 仅显式传入的字段生效(role / agent_key 不可变)。"""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=500)
    system_prompt: str | None = None


class AgentStatusUpdate(BaseModel):
    status: AgentStatus


class ToolOption(BaseModel):
    name: str
    description: str


class SkillOption(BaseModel):
    name: str
    description: str


class AgentCatalog(BaseModel):
    """新建/编辑表单的可选引用目录: 本地工具 + 当前调用者可见技能。"""

    tools: list[ToolOption]
    skills: list[SkillOption]
