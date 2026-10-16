"""智能体注册表路由: 管理页的 CRUD + 表单目录。

权限口径(见迁移 ``e7f9a3b5c1d6`` 的种子):
- ``agent:read``: 全部角色(列表/详情/目录);
- ``agent:write``: admin(通配 *)+ operator(新建/编辑/启停/删除);
- ``supervisor_main`` 为系统内置总管: 删除与暂停返回 409。

写操作落审计日志(action=``agent_create`` / ``agent_update`` / ``agent_status`` /
``agent_delete``), 与 skills 管理端同一口径。
"""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent import invalidate_runtime
from app.core.net import resolve_client_ip
from app.db.session import get_db
from app.deps import AuthContext, require_permissions
from app.exceptions import BizCode, BizError
from app.models.agent import SUPERVISOR_KEY, Agent
from app.schemas.agents import (
    AgentCatalog,
    AgentCounts,
    AgentCreate,
    AgentDetail,
    AgentStatusUpdate,
    AgentSummary,
    AgentUpdate,
    SkillOption,
    ToolOption,
)
from app.schemas.common import ok
from app.services.audit_service import record_audit
from app.skills.registry import get_skill_registry
from app.tools import ALL_TOOLS

logger = logging.getLogger("api.agents")

router = APIRouter(prefix="/agents", tags=["agents"])

READ_PERM = "agent:read"
WRITE_PERM = "agent:write"


# --------------------------------------------------------------------------- 序列化


def _counts(agent: Agent) -> AgentCounts:
    return AgentCounts(
        local=len(agent.tool_refs or []),
        mcp=len(agent.mcp_refs or []),
        rag=len(agent.rag_refs or []),
        skills=len(agent.skill_refs or []),
    )


def _summary(agent: Agent) -> AgentSummary:
    return AgentSummary(
        agent_key=agent.agent_key,
        name=agent.name,
        role=agent.role,
        status=agent.status,
        description=agent.description or "",
        counts=_counts(agent),
    )


def _detail(agent: Agent) -> AgentDetail:
    return AgentDetail(
        **_summary(agent).model_dump(),
        system_prompt=agent.system_prompt or "",
        tool_refs=list(agent.tool_refs or []),
        skill_refs=list(agent.skill_refs or []),
        mcp_refs=list(agent.mcp_refs or []),
        rag_refs=list(agent.rag_refs or []),
        sort_order=agent.sort_order or 0,
    )


def _tool_options() -> list[ToolOption]:
    return [
        ToolOption(name=t.name, description=" ".join((t.description or "").split()))
        for t in ALL_TOOLS
    ]


def _validate_refs(*, tool_refs: list[str] | None, skill_refs: list[str] | None) -> None:
    """引用必须真实存在: 未知工具/技能 → 422, 避免运行时静默丢能力。"""
    if tool_refs:
        known = {t.name for t in ALL_TOOLS}
        unknown = [key for key in tool_refs if key not in known]
        if unknown:
            raise BizError(
                BizCode.BAD_REQUEST, f"未知的本地工具: {unknown}", http_status=422
            )
    if skill_refs:
        registry = get_skill_registry()
        known = {skill.name for skill in registry.all()}
        unknown = [key for key in skill_refs if key not in known]
        if unknown:
            raise BizError(BizCode.BAD_REQUEST, f"未知的技能: {unknown}", http_status=422)


async def _fetch(db: AsyncSession, agent_key: str) -> Agent:
    agent = await db.scalar(select(Agent).where(Agent.agent_key == agent_key))
    if agent is None:
        raise BizError(BizCode.NOT_FOUND, f"智能体不存在: {agent_key}")
    return agent


async def _audit(
    db: AsyncSession,
    request: Request,
    ctx: AuthContext,
    action: str,
    agent_key: str,
    detail: dict | None = None,
) -> None:
    await record_audit(
        db,
        account_id=ctx.account_id,
        action=action,
        target_type="agent",
        target_id=agent_key,
        ip=resolve_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
        detail=detail,
    )


# --------------------------------------------------------------------------- 路由


@router.get("", summary="智能体列表(名称/ID 搜索 + 状态筛选)")
async def list_agents(
    search: str = Query(default="", max_length=64),
    status: Literal["active", "paused"] | None = Query(
        default=None, description="active / paused"
    ),
    ctx: AuthContext = Depends(require_permissions(READ_PERM)),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Agent).order_by(Agent.sort_order, Agent.id)
    if status is not None:
        stmt = stmt.where(Agent.status == status)
    if search:
        like = f"%{search.strip()}%"
        stmt = stmt.where(or_(Agent.name.ilike(like), Agent.agent_key.ilike(like)))
    rows = (await db.scalars(stmt)).all()
    return ok({"count": len(rows), "items": [_summary(agent).model_dump() for agent in rows]})


@router.get("/catalog", summary="新建/编辑表单目录(本地工具 + 可见技能)")
async def get_catalog(
    ctx: AuthContext = Depends(require_permissions(READ_PERM)),
):
    registry = get_skill_registry()
    visible = registry.visible_to(permissions=ctx.permissions, authenticated=True)
    catalog = AgentCatalog(
        tools=_tool_options(),
        skills=[
            SkillOption(name=s.name, description=s.description) for s in visible
        ],
    )
    return ok(catalog.model_dump())


@router.post("", summary="新建智能体(需 agent:write)")
async def create_agent(
    payload: AgentCreate,
    request: Request,
    ctx: AuthContext = Depends(require_permissions(WRITE_PERM)),
    db: AsyncSession = Depends(get_db),
):
    _validate_refs(tool_refs=payload.tool_refs, skill_refs=payload.skill_refs)
    exists = await db.scalar(select(Agent).where(Agent.agent_key == payload.agent_key))
    if exists is not None:
        raise BizError(BizCode.CONFLICT, f"智能体已存在: {payload.agent_key}")
    agent = Agent(**payload.model_dump())
    db.add(agent)
    await _audit(db, request, ctx, "agent_create", payload.agent_key, payload.model_dump())
    await db.commit()
    await db.refresh(agent)
    invalidate_runtime()  # 新智能体立即进入下一次调度
    logger.info("agent created key=%s by=%s", agent.agent_key, ctx.account_id)
    return ok(_detail(agent).model_dump())


@router.get("/{agent_key}", summary="智能体详情")
async def get_agent(
    agent_key: str,
    ctx: AuthContext = Depends(require_permissions(READ_PERM)),
    db: AsyncSession = Depends(get_db),
):
    agent = await _fetch(db, agent_key)
    return ok(_detail(agent).model_dump())


@router.patch("/{agent_key}", summary="编辑智能体(需 agent:write)")
async def update_agent(
    agent_key: str,
    payload: AgentUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permissions(WRITE_PERM)),
    db: AsyncSession = Depends(get_db),
):
    agent = await _fetch(db, agent_key)
    _validate_refs(tool_refs=payload.tool_refs, skill_refs=payload.skill_refs)
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(agent, field, value)
    await _audit(db, request, ctx, "agent_update", agent_key, changes)
    await db.commit()
    await db.refresh(agent)
    invalidate_runtime()
    return ok(_detail(agent).model_dump())


@router.post("/{agent_key}/status", summary="启用/暂停智能体(需 agent:write)")
async def set_agent_status(
    agent_key: str,
    payload: AgentStatusUpdate,
    request: Request,
    ctx: AuthContext = Depends(require_permissions(WRITE_PERM)),
    db: AsyncSession = Depends(get_db),
):
    agent = await _fetch(db, agent_key)
    if agent.agent_key == SUPERVISOR_KEY and payload.status == "paused":
        raise BizError(BizCode.CONFLICT, "内置总管智能体不可暂停")
    agent.status = payload.status
    await _audit(db, request, ctx, "agent_status", agent_key, {"status": payload.status})
    await db.commit()
    await db.refresh(agent)
    invalidate_runtime()  # 启停即时生效: 暂停的执行智能体不再获得 handoff 工具
    logger.info("agent status changed key=%s status=%s by=%s", agent_key, payload.status, ctx.account_id)
    return ok(_detail(agent).model_dump())


@router.delete("/{agent_key}", summary="删除智能体(需 agent:write)")
async def delete_agent(
    agent_key: str,
    request: Request,
    ctx: AuthContext = Depends(require_permissions(WRITE_PERM)),
    db: AsyncSession = Depends(get_db),
):
    agent = await _fetch(db, agent_key)
    if agent.agent_key == SUPERVISOR_KEY:
        raise BizError(BizCode.CONFLICT, "内置总管智能体不可删除")
    await db.delete(agent)
    await _audit(db, request, ctx, "agent_delete", agent_key)
    await db.commit()
    invalidate_runtime()
    logger.info("agent deleted key=%s by=%s", agent_key, ctx.account_id)
    return ok(None)
