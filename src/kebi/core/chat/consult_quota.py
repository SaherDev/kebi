"""Per-user daily consult quota — enforced in Redis.

The consult cap is a plan-tier entitlement (homebody = a few per day; paid =
unlimited). The gateway owns plans and passes the per-day limit on the
request, but it cannot enforce it: the count lives in Redis, which only this
repo touches (NestJS is barred from Redis — Constitution §V). So kebi counts
and enforces, the gateway only supplies the number.

The counter key is scoped to the UTC calendar date, so it resets naturally at
midnight UTC without a sweep — each new day mints a fresh key. The TTL only
needs to outlive the day for cleanup.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)

_KEY_PREFIX = "consults:v1:"
# The key is date-scoped, so the TTL is only a cleanup floor — two days
# comfortably outlives any single UTC day plus clock skew.
_COUNTER_TTL_SECONDS = 172_800


class ConsultQuotaService:
    """Counts consults per user per UTC day and enforces the plan-tier cap."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def check_and_increment(self, user_id: str, limit: int | None) -> bool:
        """Record one consult attempt and report whether it is within quota.

        Counts at *entry* (every attempt, before the agent runs), so a maxed
        user can't burn paid LLM spend on a turn that would be rejected
        anyway; the per-minute infra limit already bounds retry abuse.

        `limit is None` means unlimited (paid tiers, or a gateway that did
        not assert a cap) — always allowed, no Redis write. Fails *open*: if
        Redis is unreachable the consult is allowed rather than blocking a
        paying user on an infra blip.
        """
        if limit is None:
            return True
        key = f"{_KEY_PREFIX}{user_id}:{datetime.now(UTC).date().isoformat()}"
        try:
            count = int(await self._redis.incr(key))
            if count == 1:
                await self._redis.expire(key, _COUNTER_TTL_SECONDS)
        except Exception:
            logger.warning("consult quota check failed; allowing", exc_info=True)
            return True
        return count <= limit
