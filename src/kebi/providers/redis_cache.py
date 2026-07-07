"""Redis provider helpers (ADR-019, ADR-038).

`get_redis_client(url)` is a process-wide, per-URL singleton — every
caller in kebi that needs a `redis.asyncio.Redis` should obtain it
through this helper so the connection pool is shared across requests
and modules. Mirrors `_get_session_factory` in `db/session.py`.

`RedisCacheBackend` is the concrete CacheBackend implementation used by
the providers layer to satisfy the CacheBackend Protocol.
"""

from __future__ import annotations

import functools

from redis.asyncio import Redis


@functools.cache
def get_redis_client(url: str) -> Redis:
    """Return the process-wide async Redis client for `url`.

    `redis.asyncio.Redis` manages its own connection pool internally;
    sharing a single client per URL across the worker reuses that pool
    and avoids per-request TLS handshakes. Safe under uvicorn's
    one-loop-per-worker model.
    """
    client: Redis = Redis.from_url(url, decode_responses=True)
    return client


class RedisCacheBackend:
    """Wraps redis.asyncio.Redis to satisfy the CacheBackend Protocol (ADR-038).

    Construction follows ADR-072 (SSP): the Redis client is injected by the
    wiring layer (typically via `get_redis_client(url)`), never resolved
    inside this class.
    """

    def __init__(self, *, client: Redis) -> None:
        self._client = client

    async def get(self, key: str) -> str | None:
        """Return stored value for key, or None if missing/expired."""
        result: str | None = await self._client.get(key)
        return result

    async def set(self, key: str, value: str, ttl: int) -> None:
        """Store value at key with TTL in seconds."""
        await self._client.set(key, value, ex=ttl)
