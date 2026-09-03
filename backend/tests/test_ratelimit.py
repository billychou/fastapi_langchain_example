"""聊天限流令牌桶: 容量内放行, 超限 429 + Retry-After; 账号桶与 IP 桶隔离。"""
from __future__ import annotations

import fakeredis.aioredis
import pytest
from app.config import get_settings
from app.core.ratelimit import TokenBucket, enforce_chat_rate
from app.exceptions import RateLimitError


@pytest.fixture()
async def redis():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


async def test_bucket_allows_until_capacity_then_blocks(redis):
    s = get_settings()
    bucket = TokenBucket(redis)
    allowed = 0
    for _ in range(s.rl_chat_capacity + 3):
        if await bucket.allow(
            "rl:test:bucket", capacity=s.rl_chat_capacity, refill_per_min=0.0
        ):
            allowed += 1
    assert allowed == s.rl_chat_capacity


async def test_enforce_chat_rate_by_account_then_429(redis):
    s = get_settings()
    for _ in range(s.rl_chat_capacity):
        await enforce_chat_rate(redis, account_id=1, ip="1.1.1.1")
    with pytest.raises(RateLimitError) as exc_info:
        await enforce_chat_rate(redis, account_id=1, ip="9.9.9.9")
    assert exc_info.value.http_status == 429
    # 10 tokens / (6/min) = 100s
    assert exc_info.value.headers["Retry-After"] == "100"


async def test_account_exhaustion_does_not_affect_ip_bucket(redis):
    s = get_settings()
    for _ in range(s.rl_chat_capacity):
        await enforce_chat_rate(redis, account_id=1, ip="1.1.1.1")
    # 匿名请求走独立 IP 桶, 不受账号桶耗尽影响
    await enforce_chat_rate(redis, account_id=None, ip="2.2.2.2")


async def test_bucket_keys_are_scoped(redis):
    s = get_settings()
    for _ in range(s.rl_chat_capacity):
        await enforce_chat_rate(redis, account_id=1, ip="1.1.1.1")
    assert await redis.exists("rl:chat:acct:1") == 1
    assert await redis.exists("rl:chat:ip:1.1.1.1") == 0
