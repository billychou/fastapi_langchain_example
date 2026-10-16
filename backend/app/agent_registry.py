"""智能体注册表的运行期形状: MySQL ``agent`` 行 -> :class:`AgentSpec`。

运行时(``app/agent.py``)按本模块的 spec 列表构建 LangGraph 总管与各执行智能体子图;
管理页经 ``/api/v1/agents`` 读写同一张表, 写操作后调 ``app.agent.invalidate_runtime()``
让下一次聊天重建。

**无 DB 也能跑**(与 MockChatModel / 运维连接器同一哲学): 连接失败或表为空时回落到
:data:`DEFAULT_AGENT_SPECS` —— 它是迁移 ``e7f9a3b5c1d6`` 种子的逐字镜像, 由
``tests/test_migrations.py`` 的对账用例守住, 两边漂移会立刻红。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import get_engine
from app.models.agent import SUPERVISOR_KEY, Agent

logger = logging.getLogger("agent.registry")


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """一个智能体的构造配方(注册表行的不可变快照)。"""

    agent_key: str
    name: str
    role: str = "executor"
    status: str = "active"
    description: str = ""
    system_prompt: str = ""
    tool_refs: tuple[str, ...] = ()
    skill_refs: tuple[str, ...] = ()
    mcp_refs: tuple[str, ...] = ()
    rag_refs: tuple[str, ...] = ()
    sort_order: int = 0

    @property
    def is_supervisor(self) -> bool:
        return self.role == "supervisor"

    @property
    def is_active(self) -> bool:
        return self.status == "active"


def spec_from_row(row: Agent) -> AgentSpec:
    return AgentSpec(
        agent_key=row.agent_key,
        name=row.name,
        role=row.role,
        status=row.status,
        description=row.description or "",
        system_prompt=row.system_prompt or "",
        tool_refs=tuple(row.tool_refs or ()),
        skill_refs=tuple(row.skill_refs or ()),
        mcp_refs=tuple(row.mcp_refs or ()),
        rag_refs=tuple(row.rag_refs or ()),
        sort_order=row.sort_order or 0,
    )


#: 内置默认注册表: 迁移 ``e7f9a3b5c1d6`` / ``migrations/006_agents.sql`` 种子的镜像。
#: DB 不可达(测试/离线演示)或注册表为空时用它, 保证聊天入口永远存在。
#: **改种子必须同步改这里**, 对账用例见 tests/test_migrations.py。
DEFAULT_AGENT_SPECS: tuple[AgentSpec, ...] = (
    AgentSpec(
        agent_key=SUPERVISOR_KEY,
        name="调度管理者",
        role="supervisor",
        status="active",
        description="总管调度智能体，拆分复杂任务、分发至执行智能体，汇总多智能体结果，生成最终回复。",
        system_prompt=(
            "你是 AI 运维智能平台的总管调度智能体。职责: 1) 理解用户的运维请求; "
            "2) 拆分任务并调用对应执行智能体; 3) 汇总各执行智能体的结果, 给出结构化的最终回复。"
            "不要自行编造监控数据, 一切以执行智能体的返回为准。"
        ),
        tool_refs=("get_current_time",),
        skill_refs=("trip-planner",),
        sort_order=0,
    ),
    AgentSpec(
        agent_key="k8s-agent",
        name="K8s 集群诊断",
        role="executor",
        status="active",
        description="K8s 集群资源查询诊断智能体，获取集群资源信息，分析资源状态与异常事件。",
        system_prompt=(
            "你是 K8s 集群诊断智能体。用 k8s_list_resources 获取集群资源, "
            "结合 Prometheus 指标判断资源状态与异常事件, 输出诊断结论与处置建议。"
        ),
        tool_refs=("k8s_list_resources", "prometheus_query", "loki_query", "get_current_time"),
        sort_order=1,
    ),
    AgentSpec(
        agent_key="edu-agent",
        name="学院业务数据",
        role="executor",
        status="paused",
        description="学院业务数据查询智能体，通过 MCP 远端工具读取、统计学院相关业务数据。",
        system_prompt=(
            "你是学院业务数据查询智能体。基于 MCP 远端工具返回的数据回答学院教务、"
            "学员与课程相关问题; 数据缺失时明确说明, 不要推测。"
        ),
        tool_refs=("calculate",),
        skill_refs=("feynman-listener",),
        mcp_refs=("college-bi", "college-sis"),
        sort_order=2,
    ),
    AgentSpec(
        agent_key="rag-agent",
        name="知识库检索",
        role="executor",
        status="active",
        description="知识库检索智能体，检索内网知识库文档，提取资料信息，回答业务文档相关问题。",
        system_prompt=(
            "你是知识库检索智能体。检索内网知识库(运维手册、故障复盘), "
            "提取关键信息并注明出处后回答。"
        ),
        rag_refs=("ops-runbook", "fault-postmortem"),
        sort_order=3,
    ),
    AgentSpec(
        agent_key="metric-agent",
        name="指标分析",
        role="executor",
        status="active",
        description="指标分析智能体，查询 Prometheus 指标，研判趋势、识别波动，输出指标分析结果。",
        system_prompt=(
            "你是指标分析智能体。用 prometheus_query 获取指标, 研判趋势、识别波动, "
            "输出分析结论与异常点。"
        ),
        tool_refs=("prometheus_query", "calculate", "get_current_time"),
        sort_order=4,
    ),
    AgentSpec(
        agent_key="log-agent",
        name="日志分析",
        role="executor",
        status="active",
        description="日志分析智能体，查询 Loki 日志，筛选异常、定位报错，输出日志分析结果。",
        system_prompt=(
            "你是日志分析智能体。用 loki_query 筛选日志, 识别错误与异常, "
            "定位报错原因并输出分析结论。"
        ),
        tool_refs=("loki_query", "calculate", "get_current_time"),
        sort_order=5,
    ),
)


def _ensure_supervisor(specs: tuple[AgentSpec, ...]) -> tuple[AgentSpec, ...]:
    """聊天入口必须存在: 注册表里没有总管时补一个内置总管。

    运营误删/误停总管会让整个平台「能登录但说不了话」, 比多一个内置智能体更糟;
    ``supervisor_main`` 的删除与暂停在 API 层本就 409, 这里只是 DB 直改时的兜底。
    """
    if any(spec.is_supervisor for spec in specs):
        return specs
    default = next(spec for spec in DEFAULT_AGENT_SPECS if spec.is_supervisor)
    logger.warning("注册表缺少总管智能体, 已补入内置 %s", default.agent_key)
    return (default, *specs)


async def load_agent_specs(session: AsyncSession | None = None) -> tuple[AgentSpec, ...]:
    """读注册表; 任何失败(连不上/表不存在/空表)都回落到内置默认。

    ``session`` 由调用方传入时复用调用方的连接(测试里是 sqlite 内存库); 不传则
    用应用引擎现开一个会话。失败后对传入的会话做一次 rollback, 免得把它留在坏事务里。
    """
    owns_session = session is None
    own_session: AsyncSession | None = None
    if owns_session:
        try:
            factory = async_sessionmaker(get_engine(), expire_on_commit=False)
            own_session = factory()
            session = own_session
        except Exception:  # noqa: BLE001 - 引擎都建不出来, 直接默认
            logger.warning("智能体注册表不可用(引擎初始化失败), 使用内置默认注册表")
            return DEFAULT_AGENT_SPECS
    assert session is not None
    try:
        rows = (await session.scalars(select(Agent).order_by(Agent.sort_order, Agent.id))).all()
    except Exception:  # noqa: BLE001 - 未迁移/死端口/权限不足都走默认
        logger.warning("读取智能体注册表失败, 使用内置默认注册表", exc_info=True)
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return DEFAULT_AGENT_SPECS
    finally:
        if owns_session and own_session is not None:
            await own_session.close()
    if not rows:
        logger.warning("智能体注册表为空, 使用内置默认注册表")
        return DEFAULT_AGENT_SPECS
    return _ensure_supervisor(tuple(spec_from_row(row) for row in rows))


def specs_to_rows_payload(specs: tuple[AgentSpec, ...]) -> list[dict[str, Any]]:
    """测试/文档用: spec 列表转成与迁移种子同形状的字典列表。"""
    return [
        {
            "agent_key": spec.agent_key,
            "name": spec.name,
            "role": spec.role,
            "status": spec.status,
            "description": spec.description,
            "system_prompt": spec.system_prompt,
            "tool_refs": list(spec.tool_refs),
            "skill_refs": list(spec.skill_refs),
            "mcp_refs": list(spec.mcp_refs),
            "rag_refs": list(spec.rag_refs),
            "sort_order": spec.sort_order,
        }
        for spec in specs
    ]
