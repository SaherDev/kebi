"""Per-user message buffer for count-based fact-extraction debounce.

The chat layer dispatches a TurnCompleted event on every user turn. The
memory service appends each message to this buffer and only triggers
the LLM extractor on every Nth append. Redis-backed so the count is
consistent across Railway replicas; TTL refreshes on every append so
active users never expire and quiet users eventually drop their buffer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from redis.asyncio import Redis


_BUFFER_KEY_PREFIX = "memory:turns:"


_APPEND_LUA = """
redis.call('RPUSH', KEYS[1], ARGV[1])
redis.call('EXPIRE', KEYS[1], ARGV[2])
return redis.call('LLEN', KEYS[1])
"""

_DRAIN_LUA = """
local items = redis.call('LRANGE', KEYS[1], 0, -1)
redis.call('DEL', KEYS[1])
return items
"""


class MessageBuffer:
    """Redis-backed per-user message buffer.

    All ops are atomic via server-side Lua so concurrent dispatches
    (cross-replica or rapid same-user requests) can't race the count
    and slip past the threshold.
    """

    def __init__(self, redis: Redis, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds

    def _key(self, user_id: str) -> str:
        return f"{_BUFFER_KEY_PREFIX}{user_id}"

    async def append(self, user_id: str, message: str) -> int:
        """Append a message and return the new buffer length."""
        result = await cast(
            Any,
            self._redis.eval(
                _APPEND_LUA,
                1,
                self._key(user_id),
                message,
                str(self._ttl_seconds),
            ),
        )
        return int(result)

    async def drain(self, user_id: str) -> list[str]:
        """Atomically read all buffered messages and clear the key."""
        result = await cast(
            Any,
            self._redis.eval(_DRAIN_LUA, 1, self._key(user_id)),
        )
        if not result:
            return []
        return [
            item.decode() if isinstance(item, bytes) else str(item)
            for item in result
        ]

    async def cancel(self, user_id: str) -> None:
        """Drop the buffer for a user (e.g., on account deletion)."""
        await self._redis.delete(self._key(user_id))
