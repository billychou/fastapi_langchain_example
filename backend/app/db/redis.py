"""Redis 连接持有者: 应用生命周期内单例。"""
from __future__ import annotations

from redis.asyncio import Redis, from_url

from app.config import get_settings

_client: Redis | None = None


def init_redis() -> Redis:
    global _client
    if _client is None:
        s = get_settings()
        _client = from_url(s.redis_url, decode_responses=True, max_connections=50)
    return _client


def get_redis_client() -> Redis | None:
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
