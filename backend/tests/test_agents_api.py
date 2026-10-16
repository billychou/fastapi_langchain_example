"""``/api/v1/agents`` 端到端: 真 Token + 真 RBAC(内存 SQLite + fakeredis)。

夹具约束与 ``test_skills_api.py`` 同口径: ``get_db`` / ``get_redis`` 走
dependency_overrides, 全程不碰 MySQL/Redis; Access Token 真签发 + Redis 建会话。

覆盖点: 列表搜索/状态筛选与计数 chips、member 只读(operator/admin 可写)、
新建重键 409、未知工具/技能引用 422、部分更新语义、启停流、
``supervisor_main`` 禁删禁停 409、删除后 404、catalog 按调用者过滤技能。
"""
from __future__ import annotations

from dataclasses import dataclass

import fakeredis.aioredis
import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

LIST = "/api/v1/agents"
CATALOG = "/api/v1/agents/catalog"

ACCOUNTS = {"member": 1, "operator": 2, "admin": 3}


@dataclass(slots=True)
class Env:
    client: httpx.AsyncClient
    factory: async_sessionmaker[AsyncSession]
    tokens: dict[str, str]

    def auth(self, who: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[who]}"}


@pytest.fixture()
async def env():
    from app.core import tokens
    from app.db.session import Base, get_db
    from app.deps import get_redis
    from app.main import app
    from app.models import Account, AccountRole, Agent, Permission, Role, RolePermission
    from app.services.session_store import SessionStore

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with factory() as session:
        for account_id in ACCOUNTS.values():
            session.add(Account(id=account_id, login_policy="multi_device"))
        await session.flush()
        session.add_all(
            [
                Role(id=1, role_code="member", role_name="会员", is_builtin=1),
                Role(id=2, role_code="operator", role_name="运营人员", is_builtin=1),
                Role(id=3, role_code="admin", role_name="超级管理员", is_builtin=1),
                Permission(id=1, perm_code="agent:read", perm_name="查看智能体", resource="agent"),
                Permission(id=2, perm_code="agent:write", perm_name="管理智能体", resource="agent"),
                AccountRole(account_id=1, role_id=1),
                AccountRole(account_id=2, role_id=2),
                AccountRole(account_id=3, role_id=3),
                RolePermission(role_id=1, permission_id=1),
                RolePermission(role_id=2, permission_id=1),
                RolePermission(role_id=2, permission_id=2),
                Agent(
                    agent_key="supervisor_main",
                    name="调度管理者",
                    role="supervisor",
                    status="active",
                    description="总管调度智能体。",
                    system_prompt="你是总管。",
                    tool_refs=["get_current_time"],
                    skill_refs=["expense-report"],
                    mcp_refs=[],
                    rag_refs=[],
                    sort_order=0,
                ),
                Agent(
                    agent_key="k8s-agent",
                    name="K8s 集群诊断",
                    role="executor",
                    status="active",
                    description="K8s 集群资源查询诊断智能体。",
                    system_prompt="你是 K8s 诊断智能体。",
                    tool_refs=["get_current_time", "calculate"],
                    skill_refs=[],
                    mcp_refs=[],
                    rag_refs=[],
                    sort_order=1,
                ),
                Agent(
                    agent_key="edu-agent",
                    name="学院业务数据",
                    role="executor",
                    status="paused",
                    description="学院业务数据查询智能体。",
                    system_prompt="你是学院数据智能体。",
                    tool_refs=["calculate"],
                    skill_refs=[],
                    mcp_refs=["college-bi"],
                    rag_refs=[],
                    sort_order=2,
                ),
            ]
        )
        await session.commit()

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = SessionStore(redis)
    token_map: dict[str, str] = {}
    for who, account_id in ACCOUNTS.items():
        sid = f"sess-{who}"
        await store.create_session(
            account_id=account_id,
            sid=sid,
            refresh_jti=f"jti-{who}",
            ttl_seconds=3600,
            device_id="dev-pytest",
            ip="127.0.0.1",
            user_agent="pytest",
        )
        token_map[who], _ = tokens.build_token(
            account_id=account_id, session_id=sid, token_type="access", ttl_seconds=900
        )

    async def _override_db():
        async with factory() as session:
            yield session

    async def _override_redis():
        return redis

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_redis] = _override_redis
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield Env(client=client, factory=factory, tokens=token_map)

    app.dependency_overrides.clear()
    await redis.aclose()
    await engine.dispose()


def _items(body: dict) -> list[dict]:
    return body["data"]["items"]


# --------------------------------------------------------------------------- 列表


async def test_list_requires_token(env: Env):
    response = await env.client.get(LIST)
    assert response.status_code == 401
    assert response.json()["code"] == 40100


async def test_member_lists_agents_with_counts(env: Env):
    response = await env.client.get(LIST, headers=env.auth("member"))
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    items = _items(body)
    assert [item["agent_key"] for item in items] == ["supervisor_main", "k8s-agent", "edu-agent"]
    supervisor = items[0]
    assert supervisor["role"] == "supervisor" and supervisor["status"] == "active"
    assert supervisor["counts"] == {"local": 1, "mcp": 0, "rag": 0, "skills": 1}
    assert items[2]["counts"]["mcp"] == 1  # edu-agent 的 MCP 占位计数


async def test_search_matches_name_and_key(env: Env):
    response = await env.client.get(LIST, params={"search": "k8s"}, headers=env.auth("member"))
    assert [item["agent_key"] for item in _items(response.json())] == ["k8s-agent"]
    response = await env.client.get(LIST, params={"search": "学院"}, headers=env.auth("member"))
    assert [item["agent_key"] for item in _items(response.json())] == ["edu-agent"]


async def test_status_filter(env: Env):
    response = await env.client.get(LIST, params={"status": "paused"}, headers=env.auth("member"))
    assert [item["agent_key"] for item in _items(response.json())] == ["edu-agent"]
    response = await env.client.get(LIST, params={"status": "active"}, headers=env.auth("member"))
    assert len(_items(response.json())) == 2


# --------------------------------------------------------------------------- 权限


async def test_member_cannot_write(env: Env):
    payload = {"agent_key": "rogue", "name": "越权"}
    response = await env.client.post(LIST, json=payload, headers=env.auth("member"))
    assert response.status_code == 403 and response.json()["code"] == 40300
    response = await env.client.patch(f"{LIST}/k8s-agent", json={"name": "x"}, headers=env.auth("member"))
    assert response.status_code == 403
    response = await env.client.post(
        f"{LIST}/k8s-agent/status", json={"status": "paused"}, headers=env.auth("member")
    )
    assert response.status_code == 403
    response = await env.client.delete(f"{LIST}/k8s-agent", headers=env.auth("member"))
    assert response.status_code == 403


# --------------------------------------------------------------------------- 写路径


async def test_operator_creates_agent(env: Env):
    payload = {
        "agent_key": "trace-agent",
        "name": "链路分析",
        "role": "executor",
        "description": "链路追踪分析智能体。",
        "system_prompt": "你是链路分析智能体。",
        "tool_refs": ["calculate"],
        "mcp_refs": ["tempo"],
    }
    response = await env.client.post(LIST, json=payload, headers=env.auth("operator"))
    assert response.status_code == 200, response.text
    detail = response.json()["data"]
    assert detail["agent_key"] == "trace-agent"
    assert detail["counts"] == {"local": 1, "mcp": 1, "rag": 0, "skills": 0}

    listed = await env.client.get(LIST, params={"search": "trace"}, headers=env.auth("member"))
    assert [item["agent_key"] for item in _items(listed.json())] == ["trace-agent"]


async def test_create_duplicate_key_conflicts(env: Env):
    payload = {"agent_key": "k8s-agent", "name": "重复"}
    response = await env.client.post(LIST, json=payload, headers=env.auth("operator"))
    assert response.status_code == 409 and response.json()["code"] == 40900


async def test_create_rejects_unknown_refs(env: Env):
    bad_tool = {"agent_key": "bad-tool", "name": "坏工具", "tool_refs": ["no_such_tool"]}
    response = await env.client.post(LIST, json=bad_tool, headers=env.auth("operator"))
    assert response.status_code == 422
    bad_skill = {"agent_key": "bad-skill", "name": "坏技能", "skill_refs": ["no_such_skill"]}
    response = await env.client.post(LIST, json=bad_skill, headers=env.auth("operator"))
    assert response.status_code == 422


async def test_update_is_partial(env: Env):
    response = await env.client.patch(
        f"{LIST}/k8s-agent", json={"description": "新简介"}, headers=env.auth("operator")
    )
    assert response.status_code == 200
    detail = response.json()["data"]
    assert detail["description"] == "新简介"
    assert detail["name"] == "K8s 集群诊断"  # 未传字段保持原值
    assert detail["tool_refs"] == ["get_current_time", "calculate"]

    response = await env.client.patch(
        f"{LIST}/k8s-agent", json={"tool_refs": ["calculate"]}, headers=env.auth("operator")
    )
    assert response.json()["data"]["tool_refs"] == ["calculate"]


async def test_pause_and_resume(env: Env):
    response = await env.client.post(
        f"{LIST}/k8s-agent/status", json={"status": "paused"}, headers=env.auth("operator")
    )
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "paused"
    response = await env.client.post(
        f"{LIST}/k8s-agent/status", json={"status": "active"}, headers=env.auth("operator")
    )
    assert response.json()["data"]["status"] == "active"


async def test_supervisor_is_protected(env: Env):
    response = await env.client.post(
        f"{LIST}/supervisor_main/status", json={"status": "paused"}, headers=env.auth("admin")
    )
    assert response.status_code == 409 and response.json()["code"] == 40900
    response = await env.client.delete(f"{LIST}/supervisor_main", headers=env.auth("admin"))
    assert response.status_code == 409


async def test_delete_then_404(env: Env):
    response = await env.client.delete(f"{LIST}/edu-agent", headers=env.auth("operator"))
    assert response.status_code == 200
    response = await env.client.get(f"{LIST}/edu-agent", headers=env.auth("member"))
    assert response.status_code == 404 and response.json()["code"] == 40400


# --------------------------------------------------------------------------- 目录


async def test_catalog_lists_tools_and_caller_visible_skills(env: Env):
    from app.tools import ALL_TOOLS

    response = await env.client.get(CATALOG, headers=env.auth("member"))
    assert response.status_code == 200
    data = response.json()["data"]
    assert {tool["name"] for tool in data["tools"]} == {tool.name for tool in ALL_TOOLS}
    member_skills = {skill["name"] for skill in data["skills"]}
    assert "expense-report" not in member_skills  # 门禁技能对 member 不可见

    response = await env.client.get(CATALOG, headers=env.auth("admin"))
    admin_skills = {skill["name"] for skill in response.json()["data"]["skills"]}
    assert "expense-report" in admin_skills  # admin 通配 * 全可见
