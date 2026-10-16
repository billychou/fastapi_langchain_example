"""Add agents table

企业级 AI 运维智能平台的智能体注册表: 总管/执行智能体的定义、状态与工具/技能绑定。
运行时(``app/agent.py``)按本表构建 LangGraph  supervisor → executor 多智能体图;
前端智能体管理页经 ``/api/v1/agents`` 读写。

列结构与 ``app/models/agent.py`` 逐列对应; 四个 ``*_refs`` JSON 列中
``mcp_refs`` / ``rag_refs`` 为下一阶段 MCP 远端工具与 RAG 检索工具预留,
本期只建模与计数(见 ``docs/aiops-platform-design.md``)。

新建库不会重放本迁移(``migrations/004_alembic_stamp.sql`` 直接把库标记为 head),
所以同一批 DDL、智能体种子与权限种子也写进了 ``migrations/006_agents.sql``;
两条路径的一致性由 ``tests/test_migrations.py`` 守卫。

权限点:

- ``agent:read``   查看智能体注册表(全部角色)
- ``agent:write``  新建/编辑/启停/删除智能体(admin 通配 * + operator)

种子智能体取自平台首屏: ``supervisor_main``(总管)与 k8s / edu / rag / metric /
log 五个执行智能体; ``edu-agent`` 默认暂停, 演示状态筛选与启停流。

Revision ID: e7f9a3b5c1d6
Revises: f3a9c1d47b60
Create Date: 2026-10-16 10:00:00.000000

"""

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e7f9a3b5c1d6'
down_revision: str | Sequence[str] | None = 'f3a9c1d47b60'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 本迁移的数据种子在 initdb 路径上的对应文件(新建库不重放迁移, 见模块 docstring)。
INITDB_SEED_SQL: str = "006_agents.sql"

#: 本迁移创建的表(downgrade 倒序 drop)。
TABLES: tuple[str, ...] = ("agent",)

# (perm_code, perm_name, resource, api_method, api_path)
PERMISSIONS: tuple[tuple[str, str, str, str | None, str | None], ...] = (
    ("agent:read", "查看智能体注册表", "agent", "GET", "/api/v1/agents"),
    ("agent:write", "新建/编辑/启停/删除智能体", "agent", "POST", "/api/v1/agents"),
)

# 角色 → 权限: 全部角色可读; operator 与 admin(通配 *) 可管理
GRANTS: dict[str, tuple[str, ...]] = {
    "member": ("agent:read",),
    "operator": ("agent:read", "agent:write"),
}

_PERM_CODES = tuple(code for code, *_ in PERMISSIONS)

#: 平台首屏种子智能体(refs 计数即卡片 chips; 连接器工具名在下一条提交落地)。
SEED_AGENTS: tuple[dict[str, Any], ...] = (
    {
        "agent_key": "supervisor_main",
        "name": "调度管理者",
        "role": "supervisor",
        "status": "active",
        "description": "总管调度智能体，拆分复杂任务、分发至执行智能体，汇总多智能体结果，生成最终回复。",
        "system_prompt": (
            "你是 AI 运维智能平台的总管调度智能体。职责: 1) 理解用户的运维请求; "
            "2) 拆分任务并调用对应执行智能体; 3) 汇总各执行智能体的结果, 给出结构化的最终回复。"
            "不要自行编造监控数据, 一切以执行智能体的返回为准。"
        ),
        "tool_refs": ["get_current_time"],
        "skill_refs": ["trip-planner"],
        "mcp_refs": [],
        "rag_refs": [],
        "sort_order": 0,
    },
    {
        "agent_key": "k8s-agent",
        "name": "K8s 集群诊断",
        "role": "executor",
        "status": "active",
        "description": "K8s 集群资源查询诊断智能体，获取集群资源信息，分析资源状态与异常事件。",
        "system_prompt": (
            "你是 K8s 集群诊断智能体。用 k8s_list_resources 获取集群资源, "
            "结合 Prometheus 指标判断资源状态与异常事件, 输出诊断结论与处置建议。"
        ),
        "tool_refs": ["k8s_list_resources", "prometheus_query", "loki_query", "get_current_time"],
        "skill_refs": [],
        "mcp_refs": [],
        "rag_refs": [],
        "sort_order": 1,
    },
    {
        "agent_key": "edu-agent",
        "name": "学院业务数据",
        "role": "executor",
        "status": "paused",
        "description": "学院业务数据查询智能体，通过 MCP 远端工具读取、统计学院相关业务数据。",
        "system_prompt": (
            "你是学院业务数据查询智能体。基于 MCP 远端工具返回的数据回答学院教务、"
            "学员与课程相关问题; 数据缺失时明确说明, 不要推测。"
        ),
        "tool_refs": ["calculate"],
        "skill_refs": ["feynman-listener"],
        "mcp_refs": ["college-bi", "college-sis"],
        "rag_refs": [],
        "sort_order": 2,
    },
    {
        "agent_key": "rag-agent",
        "name": "知识库检索",
        "role": "executor",
        "status": "active",
        "description": "知识库检索智能体，检索内网知识库文档，提取资料信息，回答业务文档相关问题。",
        "system_prompt": (
            "你是知识库检索智能体。检索内网知识库(运维手册、故障复盘), "
            "提取关键信息并注明出处后回答。"
        ),
        "tool_refs": [],
        "skill_refs": [],
        "mcp_refs": [],
        "rag_refs": ["ops-runbook", "fault-postmortem"],
        "sort_order": 3,
    },
    {
        "agent_key": "metric-agent",
        "name": "指标分析",
        "role": "executor",
        "status": "active",
        "description": "指标分析智能体，查询 Prometheus 指标，研判趋势、识别波动，输出指标分析结果。",
        "system_prompt": (
            "你是指标分析智能体。用 prometheus_query 获取指标, 研判趋势、识别波动, "
            "输出分析结论与异常点。"
        ),
        "tool_refs": ["prometheus_query", "calculate", "get_current_time"],
        "skill_refs": [],
        "mcp_refs": [],
        "rag_refs": [],
        "sort_order": 4,
    },
    {
        "agent_key": "log-agent",
        "name": "日志分析",
        "role": "executor",
        "status": "active",
        "description": "日志分析智能体，查询 Loki 日志，筛选异常、定位报错，输出日志分析结果。",
        "system_prompt": (
            "你是日志分析智能体。用 loki_query 筛选日志, 识别错误与异常, "
            "定位报错原因并输出分析结论。"
        ),
        "tool_refs": ["loki_query", "calculate", "get_current_time"],
        "skill_refs": [],
        "mcp_refs": [],
        "rag_refs": [],
        "sort_order": 5,
    },
)

_SEED_KEYS = tuple(item["agent_key"] for item in SEED_AGENTS)

#: 与 migrations/006_agents.sql 一致的 MySQL 表选项。
_MYSQL_KWARGS: dict[str, str] = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_0900_ai_ci",
}


def _quoted(values: Sequence[str]) -> str:
    return ", ".join("'" + value.replace("'", "''") + "'" for value in values)


def _sql_literal(value: Any) -> str:
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, (list, tuple)):
        return "'" + json.dumps(list(value), ensure_ascii=False).replace("'", "''") + "'"
    return str(value)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'agent',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('agent_key', sa.String(length=64), nullable=False, comment='稳定标识, 如 supervisor_main / k8s-agent'),
        sa.Column('name', sa.String(length=64), nullable=False, comment='展示名, 如「K8s 集群诊断」'),
        sa.Column('role', sa.String(length=16), nullable=False, comment='supervisor=总管调度 / executor=执行'),
        sa.Column('status', sa.String(length=16), nullable=False, comment='active / paused'),
        sa.Column('description', sa.String(length=500), nullable=False, comment='卡片展示简介'),
        sa.Column('system_prompt', sa.Text(), nullable=False, comment='该智能体的系统提示词'),
        sa.Column('tool_refs', sa.JSON(), nullable=False, comment='本地工具名列表'),
        sa.Column('skill_refs', sa.JSON(), nullable=False, comment='技能名列表'),
        sa.Column('mcp_refs', sa.JSON(), nullable=False, comment='MCP 远端工具(预留)'),
        sa.Column('rag_refs', sa.JSON(), nullable=False, comment='RAG 检索工具(预留)'),
        sa.Column('sort_order', sa.Integer(), nullable=False, comment='卡片排序, 小在前'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('agent_key', name='uk_agent_key'),
        comment='智能体注册表: 总管/执行智能体定义与工具绑定',
        **_MYSQL_KWARGS,
    )

    columns = (
        "agent_key", "name", "role", "status", "description", "system_prompt",
        "tool_refs", "skill_refs", "mcp_refs", "rag_refs", "sort_order",
    )
    rows = ", ".join(
        "(" + ", ".join(_sql_literal(item[column]) for column in columns) + ")"
        for item in SEED_AGENTS
    )
    op.execute(
        "INSERT IGNORE INTO `agent` ("
        + ", ".join(f"`{column}`" for column in columns)
        + f") VALUES {rows}"
    )

    for code, name, resource, method, path in PERMISSIONS:
        method_sql = "NULL" if method is None else f"'{method}'"
        path_sql = "NULL" if path is None else f"'{path}'"
        op.execute(
            "INSERT IGNORE INTO `permission` "
            "(`perm_code`, `perm_name`, `resource`, `api_method`, `api_path`) VALUES "
            f"('{code}', '{name}', '{resource}', {method_sql}, {path_sql})"
        )

    for role_code, perm_codes in GRANTS.items():
        op.execute(
            "INSERT IGNORE INTO `role_permission` (`role_id`, `permission_id`) "
            "SELECT r.id, p.id FROM `role` r JOIN `permission` p "
            f"ON r.role_code = '{role_code}' AND p.perm_code IN ({_quoted(perm_codes)})"
        )


def downgrade() -> None:
    """Downgrade schema."""
    codes = _quoted(_PERM_CODES)
    granted = _quoted(sorted({code for codes_ in GRANTS.values() for code in codes_}))
    op.execute(f"DELETE FROM `agent` WHERE `agent_key` IN ({_quoted(_SEED_KEYS)})")
    op.execute(
        "DELETE rp FROM `role_permission` rp JOIN `permission` p ON p.id = rp.permission_id "
        f"WHERE p.perm_code IN ({granted})"
    )
    op.execute(f"DELETE FROM `permission` WHERE `perm_code` IN ({codes})")

    for table in reversed(TABLES):
        op.drop_table(table)
