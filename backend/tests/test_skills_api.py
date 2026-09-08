"""``/api/v1/skills`` 端到端: 真 Token + 真 RBAC(内存 SQLite + fakeredis), 技能目录在 tmp_path。

夹具约束(与 conftest 冻结的死地址配合):
- ``get_db`` / ``get_redis`` 用 dependency_overrides 指向测试资源, 全程不碰 MySQL/Redis;
- 技能注册表指向 tmp_path 下现造的目录, 并 monkeypatch 到路由模块的取用点;
- Access Token 走 ``tokens.build_token`` 真签发 + Redis 建会话, ``get_current`` 真正生效,
  权限来自 DB 里的 role/permission 关联(admin 角色按 rbac_service 规则展开为 ``*``)。

覆盖点: 按调用者过滤、无权限与不存在同样 404(不泄露技能是否存在)、``skill:admin``
才能 scope=all / 热重载(且写审计)、返回体不含技能目录的磁盘路径、SKILLS_ENABLED=false 的降级。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import fakeredis.aioredis
import httpx
import pytest
from app.skills import SkillRegistry
from sqlalchemy import BigInteger, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from skills_helpers import KNOWN_TOOLS, write_skill

LIST = "/api/v1/skills"
RELOAD = "/api/v1/skills/reload"

PAYROLL_PERM = "skill:payroll:use"
SKILL_ADMIN_PERM = "skill:admin"

# 账号 → 角色: member 无权限点; payroll 持技能门禁权限; skill_admin 持管理权限;
# admin 走 rbac_service 的内置通配(*)分支
ACCOUNTS = {"member": 1, "payroll": 2, "skill_admin": 3, "admin": 4}


@compiles(BigInteger, "sqlite")
def _sqlite_bigint_as_integer(type_, compiler, **kw):  # noqa: ANN001, ARG001
    """SQLite 只把 ``INTEGER PRIMARY KEY`` 当 rowid 别名, 否则 audit_log 插入会失败。"""
    return "INTEGER"


@dataclass(slots=True)
class Env:
    client: httpx.AsyncClient
    factory: async_sessionmaker[AsyncSession]
    redis: fakeredis.aioredis.FakeRedis
    registry: SkillRegistry
    root: Path
    tokens: dict[str, str]

    def auth(self, who: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[who]}"}


@pytest.fixture()
async def env(tmp_path, monkeypatch):
    from app.core import tokens
    from app.db.session import Base, get_db
    from app.deps import get_redis
    from app.main import app
    from app.models import Account, AccountRole, Permission, Role, RolePermission
    from app.services.session_store import SessionStore

    # ---- 技能目录: 公开 / 登录可见 / 权限门禁 各一个, 外加一个坏目录(加载失败) ----
    write_skill(
        tmp_path,
        "trip-demo",
        description="规划一日行程。",
        body="# 行程步骤\n1. 查天气\n",
        tools=["get_weather"],
        extra_files={"references/checklist.md": "带伞、带水。"},
    )
    write_skill(tmp_path, "team-notes", description="团队约定。", visibility="auth")
    write_skill(
        tmp_path,
        "payroll",
        description="计算工资。",
        visibility="permission",
        requires_permissions=[PAYROLL_PERM],
        extra_files={"references/rules.md": "个税规则"},
    )
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "SKILL.md").write_text("没有 frontmatter\n", encoding="utf-8")

    registry = SkillRegistry(tmp_path, known_tools=KNOWN_TOOLS)
    registry.reload()
    monkeypatch.setattr("app.api.v1.skills.get_skill_registry", lambda: registry)

    # ---- DB: 账号 / 角色 / 权限点 ----
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
                Role(id=2, role_code="payroll", role_name="财务", is_builtin=0),
                Role(id=3, role_code="skill_admin", role_name="技能管理员", is_builtin=0),
                Role(id=4, role_code="admin", role_name="超级管理员", is_builtin=1),
                Permission(
                    id=1, perm_code=PAYROLL_PERM, perm_name="使用工资技能", resource="skill"
                ),
                Permission(
                    id=2, perm_code=SKILL_ADMIN_PERM, perm_name="管理技能", resource="skill"
                ),
                AccountRole(id=1, account_id=1, role_id=1),
                AccountRole(id=2, account_id=2, role_id=2),
                AccountRole(id=3, account_id=3, role_id=3),
                AccountRole(id=4, account_id=4, role_id=4),
                RolePermission(role_id=2, permission_id=1),
                RolePermission(role_id=3, permission_id=2),
            ]
        )
        await session.commit()

    # ---- Redis 会话 + 真 Token ----
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
        yield Env(
            client=client,
            factory=factory,
            redis=redis,
            registry=registry,
            root=tmp_path,
            tokens=token_map,
        )

    app.dependency_overrides.clear()
    await redis.aclose()
    await engine.dispose()


def _names(body: dict) -> list[str]:
    return [item["name"] for item in body["data"]["items"]]


# --------------------------------------------------------------------------- 目录
async def test_list_requires_token(env: Env):
    response = await env.client.get(LIST)
    assert response.status_code == 401
    assert response.json()["code"] == 40100


async def test_member_sees_public_and_auth_skills_only(env: Env):
    response = await env.client.get(LIST, headers=env.auth("member"))
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0 and body["message"] == "ok"
    data = body["data"]
    assert data["enabled"] is True
    assert data["scope"] == "visible"
    assert _names(body) == ["team-notes", "trip-demo"]  # 排序稳定, 门禁技能不出现
    assert data["count"] == 2
    # 非管理端不返回加载错误(那是排障信息)
    assert "load_errors" not in data
    trip = next(i for i in data["items"] if i["name"] == "trip-demo")
    assert trip["visibility"] == "public"
    assert trip["tools"] == ["get_weather"]
    assert trip["files"] == ["references/checklist.md"]
    # 绝不泄露技能目录的物理位置
    assert str(env.root) not in response.text


async def test_permission_holder_sees_gated_skill(env: Env):
    body = (await env.client.get(LIST, headers=env.auth("payroll"))).json()
    assert _names(body) == ["payroll", "team-notes", "trip-demo"]
    payroll = next(i for i in body["data"]["items"] if i["name"] == "payroll")
    assert payroll["visibility"] == "permission"
    assert payroll["requires_permissions"] == [PAYROLL_PERM]


async def test_scope_all_requires_skill_admin(env: Env):
    for who in ("member", "payroll"):
        response = await env.client.get(f"{LIST}?scope=all", headers=env.auth(who))
        assert response.status_code == 403, who
        body = response.json()
        assert body["code"] == 40300
        assert SKILL_ADMIN_PERM in body["message"]


async def test_scope_all_exposes_gated_skills_and_load_errors(env: Env):
    body = (await env.client.get(f"{LIST}?scope=all", headers=env.auth("skill_admin"))).json()
    data = body["data"]
    assert data["scope"] == "all"
    # 坏目录加载失败, 不在 all() 里, 只出现在 load_errors
    assert _names(body) == ["payroll", "team-notes", "trip-demo"]
    assert data["count"] == len(data["items"]) == 3
    # 坏目录被跳过但原因对管理端可见
    assert data["load_errors"] and "broken" in data["load_errors"][0]


async def test_scope_all_for_wildcard_admin(env: Env):
    body = (await env.client.get(f"{LIST}?scope=all", headers=env.auth("admin"))).json()
    assert body["code"] == 0
    assert "payroll" in _names(body)
    assert body["data"]["load_errors"]


async def test_disabled_registry_degrades_to_empty_list(env: Env, tmp_path, monkeypatch):
    disabled = SkillRegistry(tmp_path, known_tools=KNOWN_TOOLS, enabled=False)
    disabled.reload()
    monkeypatch.setattr("app.api.v1.skills.get_skill_registry", lambda: disabled)

    body = (await env.client.get(LIST, headers=env.auth("admin"))).json()
    assert body["data"]["enabled"] is False
    assert body["data"]["count"] == 0
    assert body["data"]["items"] == []
    detail = await env.client.get(f"{LIST}/trip-demo", headers=env.auth("admin"))
    assert detail.status_code == 404


# --------------------------------------------------------------------------- 详情
async def test_detail_body_matches_what_the_model_reads(env: Env):
    response = await env.client.get(f"{LIST}/trip-demo", headers=env.auth("member"))
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["name"] == "trip-demo" and data["version"] == "1.2.0"
    # 与 load_skill 交给模型的文本一字不差: 前端所见即模型所得
    assert data["body"] == env.registry.render_body(env.registry.get("trip-demo"))
    assert data["body"].startswith('<skill name="trip-demo"')
    assert "read_skill_file" in data["body"]  # 正文尾部提示可用附件
    assert data["max_body_chars"] > 0
    assert "root" not in data and str(env.root) not in response.text


async def test_detail_of_gated_skill_is_404_without_permission(env: Env):
    """无权限与不存在同样 404: 不给出「这个技能存在但你没权限」的枚举信号。"""
    gated = await env.client.get(f"{LIST}/payroll", headers=env.auth("member"))
    missing = await env.client.get(f"{LIST}/no-such-skill", headers=env.auth("member"))
    assert gated.status_code == missing.status_code == 404
    assert gated.json()["code"] == missing.json()["code"] == 40400
    assert gated.json()["message"] == missing.json()["message"]


async def test_detail_of_gated_skill_allowed_with_permission(env: Env):
    holder = await env.client.get(f"{LIST}/payroll", headers=env.auth("payroll"))
    assert holder.status_code == 200
    # 管理端越过门禁排障
    admin = await env.client.get(f"{LIST}/payroll", headers=env.auth("skill_admin"))
    assert admin.status_code == 200


async def test_detail_rejects_malformed_name(env: Env):
    response = await env.client.get(f"{LIST}/NotASkillName", headers=env.auth("admin"))
    assert response.status_code == 400
    assert response.json()["code"] == 40000


# --------------------------------------------------------------------------- 附件
async def test_file_endpoint_returns_attachment(env: Env):
    response = await env.client.get(
        f"{LIST}/trip-demo/files/references/checklist.md", headers=env.auth("member")
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["name"] == "trip-demo"
    assert data["path"] == "references/checklist.md"
    assert data["content"] == "带伞、带水。"
    assert data["chars"] == len(data["content"])


async def test_file_endpoint_requires_skill_permission(env: Env):
    response = await env.client.get(
        f"{LIST}/payroll/files/references/rules.md", headers=env.auth("member")
    )
    assert response.status_code == 404
    allowed = await env.client.get(
        f"{LIST}/payroll/files/references/rules.md", headers=env.auth("payroll")
    )
    assert allowed.status_code == 200
    assert allowed.json()["data"]["content"] == "个税规则"


@pytest.mark.parametrize(
    "bad_path",
    [
        "..%2F..%2F..%2Fetc%2Fpasswd",  # URL 编码的穿越
        "references",  # 目录而非文件
        "references/nope.md",  # 不存在
        "%2Fetc%2Fpasswd",  # 绝对路径
    ],
)
async def test_file_endpoint_blocks_bad_paths(env: Env, bad_path: str):
    response = await env.client.get(
        f"{LIST}/trip-demo/files/{bad_path}", headers=env.auth("admin")
    )
    assert response.status_code == 404, bad_path
    body = response.json()
    # 必须是技能路由的 BizError(而不是路由未命中的 {"detail": "Not Found"})
    assert body["code"] == 40400 and body["message"] == "技能附件不存在", body
    assert str(env.root) not in response.text  # 不泄露真实文件系统信息


# --------------------------------------------------------------------------- 热重载
async def test_reload_requires_authentication_and_skill_admin(env: Env):
    anonymous = await env.client.post(RELOAD)
    assert anonymous.status_code == 401
    assert anonymous.json()["code"] == 40100

    for who in ("member", "payroll"):
        response = await env.client.post(RELOAD, headers=env.auth(who))
        assert response.status_code == 403, who
        assert response.json()["code"] == 40300


async def test_reload_rescans_and_audits(env: Env):
    from app.models import AuditLog

    write_skill(env.root, "invoice", description="开发票。")
    response = await env.client.post(RELOAD, headers=env.auth("skill_admin"))
    assert response.status_code == 200
    data = response.json()["data"]
    assert "invoice" in data["names"] and data["count"] == len(data["names"])
    assert data["errors"] and "broken" in data["errors"][0]

    # 重载后新技能立刻出现在目录里(无需重启进程)
    listed = (await env.client.get(LIST, headers=env.auth("skill_admin"))).json()
    assert "invoice" in _names(listed)

    async with env.factory() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    assert len(rows) == 1
    assert rows[0].action == "skill_reload"
    assert rows[0].target_type == "skill"
    assert rows[0].account_id == ACCOUNTS["skill_admin"]
    assert rows[0].detail["count"] == data["count"]


async def test_reload_by_wildcard_admin_is_audited_too(env: Env):
    from app.models import AuditLog

    response = await env.client.post(RELOAD, headers=env.auth("admin"))
    assert response.status_code == 200
    async with env.factory() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    assert [r.account_id for r in rows] == [ACCOUNTS["admin"]]
