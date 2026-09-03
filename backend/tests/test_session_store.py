"""Redis 会话仓库: Refresh CAS 轮换(重放检测)/撤销/黑名单, 基于 fakeredis。"""
from __future__ import annotations

import fakeredis.aioredis
import pytest
from app.services.session_store import SessionStore, new_session_id


@pytest.fixture()
async def store():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield SessionStore(redis)
    await redis.aclose()


async def _create(store: SessionStore, sid: str | None = None, jti: str = "j0") -> str:
    sid = sid or new_session_id()
    await store.create_session(
        account_id=1,
        sid=sid,
        refresh_jti=jti,
        ttl_seconds=3600,
        device_id="dev-1",
        ip="1.2.3.4",
        user_agent="ua",
    )
    return sid


async def test_create_and_get(store):
    sid = await _create(store)
    data = await store.get_session(1, sid)
    assert data is not None
    assert data["refresh_jti"] == "j0"
    assert data["ip"] == "1.2.3.4"
    assert await store.get_session(1, "missing-sid") is None


async def test_rotate_cas_detects_replay(store):
    sid = await _create(store, jti="j0")
    assert await store.rotate_refresh_jti(1, sid, "j0", "j1", 3600) is True
    # 拿旧 jti 重放: CAS 失败, 会话保留等待上层撤销决策
    assert await store.rotate_refresh_jti(1, sid, "j0", "j2", 3600) is False
    # 新 jti 继续轮换正常
    assert await store.rotate_refresh_jti(1, sid, "j1", "j3", 3600) is True
    data = await store.get_session(1, sid)
    assert data["refresh_jti"] == "j3"


async def test_rotate_missing_session(store):
    assert await store.rotate_refresh_jti(1, "nope", "j0", "j1", 3600) is False


async def test_list_and_revoke(store):
    await _create(store, sid="s1")
    await _create(store, sid="s2")
    sessions = await store.list_sessions(1)
    assert {s["sid"] for s in sessions} == {"s1", "s2"}

    await store.revoke_session(1, "s1")
    assert await store.get_session(1, "s1") is None
    assert {s["sid"] for s in await store.list_sessions(1)} == {"s2"}

    revoked = await store.revoke_all_sessions(1)
    assert revoked == ["s2"]
    assert await store.list_sessions(1) == []


async def test_kick_other_sessions(store):
    await _create(store, sid="keep")
    await _create(store, sid="kick1")
    await _create(store, sid="kick2")
    kicked = await store.kick_other_sessions(1, keep_sid="keep")
    assert set(kicked) == {"kick1", "kick2"}
    assert await store.get_session(1, "keep") is not None


async def test_access_blacklist(store):
    await store.blacklist_access_jti("jti-x", ttl_seconds=60)
    assert await store.is_access_blacklisted("jti-x") is True
    assert await store.is_access_blacklisted("jti-y") is False
    # 剩余有效期 <=0 时不写黑名单(黑名单条目随 Token 过期自动清理)
    await store.blacklist_access_jti("jti-z", ttl_seconds=0)
    assert await store.is_access_blacklisted("jti-z") is False
