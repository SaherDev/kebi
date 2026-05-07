"""RedisCacheBackend — concrete CacheBackend implementation using redis[asyncio].

This is the only file in kebi that imports from redis directly (ADR-038).
All other modules depend on the CacheBackend Protocol from providers/cache.py.
"""

from __future__ import annotations

from redis.asyncio import Redis


class RedisCacheBackend:
    """Wraps redis.asyncio.Redis to satisfy the CacheBackend Protocol (ADR-038)."""

    def __init__(self, url: str) -> None:
        self._client: Redis = Redis.from_url(url, decode_responses=True)

    async def get(self, key: str) -> str | None:
        """Return stored value for key, or None if missing/expired."""
        result: str | None = await self._client.get(key)
        return result

    async def set(self, key: str, value: str, ttl: int) -> None:
        """Store value at key with TTL in seconds."""
        await self._client.set(key, value, ex=ttl)
