"""/api/v1/account/* 自服务接口端到端: 内存 SQLite + fakeredis, 真 Token 真会话。

夹具约束(与 conftest 冻结的死地址配合):
- `get_db` / `get_redis` 用 dependency_overrides 指向测试资源, 全程不碰 MySQL/Redis;
- 种子数据显式给主键 id —— SQLite 下 BigInteger 自增不生效;
- Access Token 走 `tokens.build_token` 真签发, 并在 Redis 建会话,
  这样 `get_current`(strict_session_check=True)是真正生效的, 而不是被绕过。
"""
from __future__ import annotations

from dataclasses import dataclass

import fakeredis.aioredis
import httpx
import pytest
from sqlalchemy import BigInteger, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

ME = "/api/v1/account/me"
PROFILE = "/api/v1/account/profile"
POLICY = "/api/v1/account/login-policy"


@compiles(BigInteger, "sqlite")
def _sqlite_bigint_as_integer(type_, compiler, **kw):  # noqa: ANN001, ARG001
    """SQLite 只把 `INTEGER PRIMARY KEY` 当 rowid 别名, `BIGINT` 主键不会自增。

    模型为对齐 MySQL 统一声明 BigInteger; 这里只在 sqlite 方言层面改写渲染结果,
    使 audit_log / user_profile 这类自增主键在内存库里能正常 INSERT
    (否则 NOT NULL 失败会被 audit_service 吞掉并回滚, 表现为难查的 MissingGreenlet)。
    """
    return "INTEGER"


@dataclass(slots=True)
class Env:
    client: httpx.AsyncClient
    factory: async_sessionmaker[AsyncSession]
    redis: fakeredis.aioredis.FakeRedis
    token: str
    sid: str

    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


@pytest.fixture()
async def env():
    from app.core import tokens
    from app.db.session import Base, get_db
    from app.deps import get_redis
    from app.main import app
    from app.models import Account, AccountRole, Role, UserProfile
    from app.services.session_store import SessionStore

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with factory() as session:
        session.add(Account(id=1, login_policy="multi_device"))
        await session.flush()
        session.add(
            UserProfile(
                id=1,
                account_id=1,
                nickname="小明",
                avatar_url="https://cdn.example.com/a.png",
                phone_masked="138****0000",
                email_masked="x***@example.com",
            )
        )
        session.add(Role(id=1, role_code="member", role_name="会员", is_builtin=1))
        session.add(AccountRole(id=1, account_id=1, role_id=1))
        await session.commit()

    sid = "sess-test-1"
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await SessionStore(redis).create_session(
        account_id=1,
        sid=sid,
        refresh_jti="jti-refresh",
        ttl_seconds=3600,
        device_id="dev-pytest",
        ip="127.0.0.1",
        user_agent="pytest",
    )
    token, _ = tokens.build_token(
        account_id=1, session_id=sid, token_type="access", ttl_seconds=900
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
        yield Env(client=client, factory=factory, redis=redis, token=token, sid=sid)

    app.dependency_overrides.clear()
    await redis.aclose()
    await engine.dispose()


async def test_me_returns_masked_profile_roles_and_policy(env: Env):
    response = await env.client.get(ME, headers=env.auth())
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["nickname"] == "小明"
    assert data["phone_masked"] == "138****0000"
    assert data["email_masked"] == "x***@example.com"
    assert data["roles"] == ["member"]
    assert data["login_policy"] == "multi_device"
    assert data["account_uuid"]
    # PII 密文/盲索引绝不能出现在响应里
    assert "phone_enc" not in data and "phone_hash" not in data


async def test_me_requires_token(env: Env):
    response = await env.client.get(ME)
    assert response.status_code == 401
    assert response.json()["code"] == 40100


async def test_patch_profile_updates_nickname_and_avatar(env: Env):
    response = await env.client.patch(
        PROFILE,
        headers=env.auth(),
        json={"nickname": "  小红  ", "avatar_url": "https://cdn.example.com/b.png"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["nickname"] == "小红"  # 前后空白被 strip
    assert data["avatar_url"] == "https://cdn.example.com/b.png"
    assert data["phone_masked"] == "138****0000"  # 未提交字段保持不变

    me = (await env.client.get(ME, headers=env.auth())).json()["data"]
    assert me["nickname"] == "小红"
    assert me["avatar_url"] == "https://cdn.example.com/b.png"


async def test_patch_profile_empty_avatar_clears_it(env: Env):
    response = await env.client.patch(PROFILE, headers=env.auth(), json={"avatar_url": ""})
    assert response.status_code == 200
    assert response.json()["data"]["avatar_url"] is None
    me = (await env.client.get(ME, headers=env.auth())).json()["data"]
    assert me["avatar_url"] is None


async def test_patch_profile_rejects_non_http_avatar(env: Env):
    for bad in ("ftp://cdn.example.com/a.png", "javascript:alert(1)", "/local/a.png"):
        response = await env.client.patch(PROFILE, headers=env.auth(), json={"avatar_url": bad})
        assert response.status_code == 400
        body = response.json()
        assert body["code"] == 40000
        assert "http" in body["message"]
    # 校验失败不应污染已有资料
    me = (await env.client.get(ME, headers=env.auth())).json()["data"]
    assert me["avatar_url"] == "https://cdn.example.com/a.png"


async def test_patch_profile_rejects_oversized_nickname(env: Env):
    response = await env.client.patch(PROFILE, headers=env.auth(), json={"nickname": "n" * 65})
    assert response.status_code == 400
    assert response.json()["code"] == 40000


async def test_patch_profile_rejects_blank_nickname(env: Env):
    response = await env.client.patch(PROFILE, headers=env.auth(), json={"nickname": "   "})
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == 40000
    assert "昵称" in body["message"]


async def test_patch_profile_empty_body_is_noop(env: Env):
    response = await env.client.patch(PROFILE, headers=env.auth(), json={})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["nickname"] == "小明"
    assert data["avatar_url"] == "https://cdn.example.com/a.png"

    from app.models import AuditLog

    async with env.factory() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    assert rows == []  # no-op 不产生审计噪声


async def test_patch_profile_requires_token(env: Env):
    response = await env.client.patch(PROFILE, json={"nickname": "x"})
    assert response.status_code == 401
    assert response.json()["code"] == 40100


async def test_patch_profile_writes_audit_log(env: Env):
    response = await env.client.patch(PROFILE, headers=env.auth(), json={"nickname": "审计名"})
    assert response.status_code == 200

    from app.models import AuditLog

    async with env.factory() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.account_id == 1
    assert row.action == "profile_update"
    assert row.target_type == "account"
    assert row.target_id == "1"
    assert row.detail == {"fields": ["nickname"]}


async def test_patch_profile_creates_missing_profile_row(env: Env):
    """历史数据/OAuth 首登可能没有 user_profile 行: 更新时补建而不是 500。"""
    from app.models import Account

    async with env.factory() as session:
        account = await session.get(Account, 1)
        account.profile = None  # cascade delete-orphan
        await session.commit()

    response = await env.client.patch(PROFILE, headers=env.auth(), json={"nickname": "新资料"})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["nickname"] == "新资料"
    assert data["phone_masked"] is None  # 新建行没有 PII


async def test_update_profile_unknown_account_raises_not_found(env: Env):
    from app.exceptions import BizError
    from app.services import account_service
    from app.services.auth_service import ClientMeta

    async with env.factory() as session:
        with pytest.raises(BizError) as exc_info:
            await account_service.update_profile(
                session,
                env.redis,
                account_id=999,
                nickname="幽灵",
                avatar_url=None,
                meta=ClientMeta(ip="127.0.0.1"),
            )
    assert int(exc_info.value.code) == 40400


async def _add_session(env: Env, sid: str) -> None:
    """再开一个在线会话(模拟另一台设备), 用于验证单端互踢。"""
    from app.services.session_store import SessionStore

    await SessionStore(env.redis).create_session(
        account_id=1,
        sid=sid,
        refresh_jti=f"jti-{sid}",
        ttl_seconds=3600,
        device_id="dev-other",
        ip="10.0.0.9",
        user_agent="other-device",
    )


async def test_put_login_policy_single_device_kicks_other_sessions(env: Env):
    from app.services.session_store import SessionStore

    await _add_session(env, "sess-other")
    store = SessionStore(env.redis)
    assert await store.get_session(1, "sess-other") is not None

    response = await env.client.put(POLICY, headers=env.auth(), json={"policy": "single_device"})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["login_policy"] == "single_device"
    assert data["kicked_sessions"] == 1

    # 其他设备会话被撤销, 当前会话保留
    assert await store.get_session(1, "sess-other") is None
    assert await store.get_session(1, env.sid) is not None

    # 策略落库 + /me 回读一致
    from app.models import Account

    async with env.factory() as session:
        account = await session.get(Account, 1)
        assert account.login_policy == "single_device"
    me = (await env.client.get(ME, headers=env.auth())).json()["data"]
    assert me["login_policy"] == "single_device"


async def test_put_login_policy_multi_device_keeps_sessions(env: Env):
    from app.services.session_store import SessionStore

    await _add_session(env, "sess-other")
    response = await env.client.put(POLICY, headers=env.auth(), json={"policy": "multi_device"})
    assert response.status_code == 200
    assert response.json()["data"]["kicked_sessions"] == 0

    store = SessionStore(env.redis)
    assert await store.get_session(1, "sess-other") is not None
    assert await store.get_session(1, env.sid) is not None


async def test_put_login_policy_rejects_unknown_value(env: Env):
    response = await env.client.put(POLICY, headers=env.auth(), json={"policy": "nope"})
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == 40000
    assert "policy" in body["message"]

    # query 参数写法不再被接受(已切换为 JSON body)
    legacy = await env.client.put(f"{POLICY}?policy=single_device", headers=env.auth())
    assert legacy.status_code == 400
    assert legacy.json()["code"] == 40000


async def test_put_login_policy_requires_token(env: Env):
    response = await env.client.put(POLICY, json={"policy": "single_device"})
    assert response.status_code == 401
    assert response.json()["code"] == 40100


async def test_put_login_policy_writes_audit_log(env: Env):
    from app.models import AuditLog

    await env.client.put(POLICY, headers=env.auth(), json={"policy": "single_device"})
    async with env.factory() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    assert len(rows) == 1
    assert rows[0].action == "login_policy_change"
    assert rows[0].detail == {"policy": "single_device", "kicked_sessions": 0}


async def test_set_login_policy_unknown_account_raises_not_found(env: Env):
    from app.exceptions import BizError
    from app.services import account_service
    from app.services.auth_service import ClientMeta

    async with env.factory() as session:
        with pytest.raises(BizError) as exc_info:
            await account_service.set_login_policy(
                session,
                env.redis,
                account_id=999,
                session_id=env.sid,
                policy="single_device",
                meta=ClientMeta(ip="127.0.0.1"),
            )
    assert int(exc_info.value.code) == 40400
