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

# Per-message and per-buffer caps. A single message can't push more than
# 1000 chars into the buffer (longer is truncated). The buffer itself
# holds at most 50 messages — once full, further appends are dropped
# atomically inside Redis. Both numbers are headroom over the normal
# debounce_messages=5 threshold (config/app.yaml) so a healthy user is
# never close to them; they exist to bound adversarial growth, not to
# shape product behavior.
_MAX_MESSAGE_CHARS = 1000
_MAX_BUFFER_LEN = 50


_APPEND_LUA = """
local cur = redis.call('LLEN', KEYS[1])
if tonumber(cur) >= tonumber(ARGV[3]) then
  return tonumber(cur)
end
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
        """Append a message and return the new buffer length.

        Per-message length is capped client-side; the per-buffer length
        cap is enforced atomically inside the Lua script so concurrent
        appends from multiple replicas cannot overshoot.
        """
        clipped = message[:_MAX_MESSAGE_CHARS]
        result = await cast(
            Any,
            self._redis.eval(
                _APPEND_LUA,
                1,
                self._key(user_id),
                clipped,
                str(self._ttl_seconds),
                str(_MAX_BUFFER_LEN),
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
            item.decode() if isinstance(item, bytes) else str(item) for item in result
        ]

    async def cancel(self, user_id: str) -> None:
        """Drop the buffer for a user (e.g., on account deletion)."""
        await self._redis.delete(self._key(user_id))
