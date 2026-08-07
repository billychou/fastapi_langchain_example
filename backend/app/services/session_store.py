"""Redis 会话仓库: Refresh Token 服务端状态、主动撤销与黑名单。

Key 设计:
- auth:sess:{account_id}:{sid}   HASH  会话元数据(device/ip/ua/refresh_jti/...)
- auth:sess-idx:{account_id}     SET   该账号全部 sid(踢人/登出全端时枚举)
- auth:blk:{jti}                 STR   Access Token jti 黑名单, TTL=剩余有效期

Refresh Token 轮换采用 Lua CAS: 仅当会话内 refresh_jti 与请求一致才允许轮换;
不一致说明旧 Refresh Token 被重放(可能泄露) → 撤销整个会话强制重新登录。
"""
from __future__ import annotations

import time
import uuid

from redis.asyncio import Redis

_ROTATE_LUA = """
local cur = redis.call('HGET', KEYS[1], 'refresh_jti')
if cur == false or cur ~= ARGV[1] then
  return 0
end
redis.call('HSET', KEYS[1], 'refresh_jti', ARGV[2])
redis.call('HSET', KEYS[1], 'last_seen', ARGV[3])
redis.call('EXPIRE', KEYS[1], ARGV[4])
return 1
"""

# 会话索引比会话本身多保留 1 天, 便于兜底清理
_INDEX_EXTRA_TTL = 86400


class SessionStore:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._rotate = redis.register_script(_ROTATE_LUA)

    # ---------------- key helpers ----------------
    @staticmethod
    def _sess_key(account_id: int, sid: str) -> str:
        return f"auth:sess:{account_id}:{sid}"

    @staticmethod
    def _index_key(account_id: int) -> str:
        return f"auth:sess-idx:{account_id}"

    @staticmethod
    def _blk_key(jti: str) -> str:
        return f"auth:blk:{jti}"

    # ---------------- lifecycle ----------------
    async def create_session(
        self,
        *,
        account_id: int,
        sid: str,
        refresh_jti: str,
        ttl_seconds: int,
        device_id: str | None,
        ip: str,
        user_agent: str | None,
    ) -> None:
        key = self._sess_key(account_id, sid)
        now = str(int(time.time()))
        await self._redis.hset(
            key,
            mapping={
                "sid": sid,
                "device_id": device_id or "",
                "ip": ip,
                "user_agent": (user_agent or "")[:512],
                "refresh_jti": refresh_jti,
                "created_at": now,
                "last_seen": now,
            },
        )
        await self._redis.expire(key, ttl_seconds)
        idx = self._index_key(account_id)
        await self._redis.sadd(idx, sid)
        await self._redis.expire(idx, ttl_seconds + _INDEX_EXTRA_TTL)

    async def get_session(self, account_id: int, sid: str) -> dict[str, str] | None:
        data = await self._redis.hgetall(self._sess_key(account_id, sid))
        return data or None

    async def list_sessions(self, account_id: int) -> list[dict[str, str]]:
        idx = self._index_key(account_id)
        sids = await self._redis.smembers(idx)
        sessions: list[dict[str, str]] = []
        for sid in sids:
            data = await self.get_session(account_id, sid)
            if data is None:  # 会话已过期, 清理悬挂索引
                await self._redis.srem(idx, sid)
                continue
            sessions.append(data)
        sessions.sort(key=lambda d: d.get("created_at", ""), reverse=True)
        return sessions

    async def rotate_refresh_jti(
        self, account_id: int, sid: str, presented_jti: str, new_jti: str, ttl_seconds: int
    ) -> bool:
        """CAS 轮换: True=成功; False=重放或会话不存在。"""
        result = await self._rotate(
            keys=[self._sess_key(account_id, sid)],
            args=[presented_jti, new_jti, str(int(time.time())), ttl_seconds],
        )
        if result:
            await self._redis.expire(self._index_key(account_id), ttl_seconds + _INDEX_EXTRA_TTL)
        return bool(result)

    async def revoke_session(self, account_id: int, sid: str) -> None:
        await self._redis.delete(self._sess_key(account_id, sid))
        await self._redis.srem(self._index_key(account_id), sid)

    async def revoke_all_sessions(self, account_id: int) -> list[str]:
        idx = self._index_key(account_id)
        sids = list(await self._redis.smembers(idx))
        if sids:
            await self._redis.delete(*[self._sess_key(account_id, sid) for sid in sids])
            await self._redis.delete(idx)
        return sids

    async def kick_other_sessions(self, account_id: int, keep_sid: str) -> list[str]:
        """单端互踢: 保留当前会话, 撤销其余全部会话。"""
        kicked: list[str] = []
        for sid in await self._redis.smembers(self._index_key(account_id)):
            if sid != keep_sid:
                await self.revoke_session(account_id, sid)
                kicked.append(sid)
        return kicked

    # ---------------- access token 黑名单 ----------------
    async def blacklist_access_jti(self, jti: str, ttl_seconds: int) -> None:
        if ttl_seconds > 0:
            await self._redis.set(self._blk_key(jti), "1", ex=ttl_seconds)

    async def is_access_blacklisted(self, jti: str) -> bool:
        return await self._redis.exists(self._blk_key(jti)) > 0


def new_session_id() -> str:
    return uuid.uuid4().hex
