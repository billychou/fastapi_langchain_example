"""Redis 令牌桶限流 + 登录失败锁定(指数退避)。

- 令牌桶使用 Lua 脚本保证 check-and-set 原子性, 多实例部署安全。
- 登录失败计数与锁定独立于令牌桶: 窗口内连续失败触发账号维度锁定。
"""
from __future__ import annotations

import time

from redis.asyncio import Redis

from app.config import get_settings
from app.exceptions import AuthError, BizCode, RateLimitError

# 令牌桶: 返回 1=放行 0=限流
_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_per_ms = tonumber(ARGV[2]) / 1000
local now = tonumber(ARGV[3])
local req = tonumber(ARGV[4])
local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then
  tokens = capacity
  ts = now
end
local delta = math.max(0, now - ts)
tokens = math.min(capacity, tokens + delta * refill_per_ms)
local allowed = 0
if tokens >= req then
  tokens = tokens - req
  allowed = 1
end
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
local ttl_ms = math.ceil(capacity / math.max(refill_per_ms, 1e-9)) + 60000
redis.call('PEXPIRE', key, ttl_ms)
return allowed
"""


class TokenBucket:
    """分布式令牌桶: key 维度独立, 容量与速率按场景配置。"""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._script = redis.register_script(_TOKEN_BUCKET_LUA)

    async def allow(self, key: str, *, capacity: int, refill_per_min: float, cost: int = 1) -> bool:
        now_ms = int(time.time() * 1000)
        result = await self._script(
            keys=[key], args=[capacity, refill_per_min / 60.0, now_ms, cost]
        )
        return bool(result)


def _retry_after_hint(capacity: int, refill_per_min: float) -> int:
    return max(1, int(capacity / max(refill_per_min, 1e-9) * 60))


async def enforce_login_rate(
    redis: Redis, *, ip: str, identifier: str | None
) -> None:
    """登录接口: IP 桶 + 账号(标识)桶双重限流, 超限抛 429 + Retry-After。"""
    s = get_settings()
    bucket = TokenBucket(redis)
    if not await bucket.allow(
        f"rl:login:ip:{ip}",
        capacity=s.rl_login_ip_capacity,
        refill_per_min=s.rl_login_ip_refill_per_min,
    ):
        raise RateLimitError(
            "当前来源请求过于频繁", _retry_after_hint(s.rl_login_ip_capacity, s.rl_login_ip_refill_per_min)
        )
    if identifier:
        if not await bucket.allow(
            f"rl:login:id:{identifier}",
            capacity=s.rl_login_account_capacity,
            refill_per_min=s.rl_login_account_refill_per_min,
        ):
            raise RateLimitError(
                "该账号尝试过于频繁",
                _retry_after_hint(s.rl_login_account_capacity, s.rl_login_account_refill_per_min),
            )


async def enforce_sms_rate(redis: Redis, *, ip: str, phone: str) -> None:
    """验证码发送: IP 桶 + 手机号分钟级桶 + 每日配额(固定窗口)。"""
    s = get_settings()
    bucket = TokenBucket(redis)
    if not await bucket.allow(
        f"rl:sms:ip:{ip}", capacity=s.rl_sms_ip_capacity, refill_per_min=s.rl_sms_ip_refill_per_min
    ):
        raise RateLimitError("验证码发送过于频繁(IP)", 60)
    if not await bucket.allow(f"rl:sms:min:{phone}", capacity=s.rl_sms_per_minute, refill_per_min=s.rl_sms_per_minute):
        raise RateLimitError("验证码发送过于频繁, 请 1 分钟后再试", 60)
    day_key = f"rl:sms:day:{phone}"
    used = await redis.incr(day_key)
    if used == 1:
        await redis.expire(day_key, 86400)
    if used > s.rl_sms_per_day:
        raise RateLimitError("今日验证码次数已用尽", 3600)


class LoginGuard:
    """登录失败锁定: 窗口计数 → 锁定, 锁定时长随锁定次数指数递增。"""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._s = get_settings()

    def _keys(self, identity: str) -> tuple[str, str, str]:
        return (
            f"auth:fail:{identity}",
            f"auth:lock:{identity}",
            f"auth:lockn:{identity}",
        )

    async def assert_not_locked(self, identity: str) -> None:
        fail_key, lock_key, _ = self._keys(identity)
        ttl = await self._redis.ttl(lock_key)
        if ttl and ttl > 0:
            raise AuthError(
                BizCode.ACCOUNT_LOCKED,
                f"账号已临时锁定, 请 {ttl} 秒后重试",
                headers={"Retry-After": str(ttl)},
            )

    def fail_count_key(self, identity: str) -> str:
        return self._keys(identity)[0]

    async def fail_count(self, identity: str) -> int:
        return int(await self._redis.get(self._keys(identity)[0]) or 0)

    async def register_failure(self, identity: str) -> None:
        """失败 +1; 达到阈值触发锁定(指数退避), 并清理失败计数。"""
        s = self._s
        fail_key, lock_key, lockn_key = self._keys(identity)
        count = await self._redis.incr(fail_key)
        if count == 1:
            await self._redis.expire(fail_key, s.login_fail_window_seconds)
        if count < s.login_max_failures:
            return
        lock_times = await self._redis.incr(lockn_key)
        if lock_times == 1:
            await self._redis.expire(lockn_key, 86400)
        lock_seconds = min(s.login_lock_base_seconds * (2 ** max(lock_times - 1, 0)), s.login_lock_max_seconds)
        await self._redis.set(lock_key, "1", ex=lock_seconds)
        await self._redis.delete(fail_key)

    async def reset(self, identity: str) -> None:
        fail_key, lock_key, lockn_key = self._keys(identity)
        await self._redis.delete(fail_key, lock_key, lockn_key)
