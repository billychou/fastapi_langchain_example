"""智能体注册表: 平台「智能体」(supervisor / executor) 的元数据。

运行时(``app/agent.py``)按本表构建 LangGraph 总管与各执行智能体子图;
前端智能体管理页经 ``/api/v1/agents`` 读写本表。

- ``tool_refs`` / ``skill_refs`` 引用本地工具目录(``ALL_TOOLS`` 的函数名)与
  技能目录(``skills/`` 下目录名), 写接口会校验引用真实存在;
- ``mcp_refs`` / ``rag_refs`` 为 MCP 远端工具与 RAG 检索工具预留: 本期只建模与
  计数, 真实客户端下一阶段接入(见 ``docs/aiops-platform-design.md``);
- ``supervisor_main`` 是系统内置总管: 不可删除、不可暂停。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base

#: 内置总管智能体的 agent_key: 删除/暂停接口对它会返回 409。
SUPERVISOR_KEY = "supervisor_main"

AGENT_ROLES = ("supervisor", "executor")
AGENT_STATUSES = ("active", "paused")


class Agent(Base):
    """智能体定义: 角色 / 状态 / 提示词 / 工具与技能绑定。"""

    __tablename__ = "agent"
    __table_args__ = (UniqueConstraint("agent_key", name="uk_agent_key"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    agent_key: Mapped[str] = mapped_column(
        String(64), comment="稳定标识, 如 supervisor_main / k8s-agent"
    )
    name: Mapped[str] = mapped_column(String(64), comment="展示名, 如「K8s 集群诊断」")
    role: Mapped[str] = mapped_column(String(16), comment="supervisor=总管调度 / executor=执行")
    status: Mapped[str] = mapped_column(String(16), default="active", comment="active / paused")
    description: Mapped[str] = mapped_column(String(500), default="", comment="卡片展示简介")
    system_prompt: Mapped[str] = mapped_column(Text, default="", comment="该智能体的系统提示词")
    tool_refs: Mapped[list[Any]] = mapped_column(JSON, default=list, comment="本地工具名列表")
    skill_refs: Mapped[list[Any]] = mapped_column(JSON, default=list, comment="技能名列表")
    mcp_refs: Mapped[list[Any]] = mapped_column(JSON, default=list, comment="MCP 远端工具(预留)")
    rag_refs: Mapped[list[Any]] = mapped_column(JSON, default=list, comment="RAG 检索工具(预留)")
    sort_order: Mapped[int] = mapped_column(Integer, default=0, comment="卡片排序, 小在前")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), server_onupdate=func.now()
    )
