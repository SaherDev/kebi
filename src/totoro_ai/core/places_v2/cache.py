"""RedisPlacesCache — flat PlaceObject cache keyed by provider_id.

Cache key: `place_v2:{provider_id}`. TTL: 30 days (2_592_000 s) by default.
Live fields only (rating, hours, phone, website, popularity, business_status,
cached_at). The full PlaceObject is stored so overlays are a simple JSON
parse + field copy.
"""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING

from .models import PlaceObject
from .protocols import PLACE_CACHE_TTL_SECONDS

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)

_KEY_PREFIX = "place_v2:"


@functools.cache
def _shared_redis_client(url: str) -> Redis:
    """Process-wide Redis client per URL — shares the connection pool
    across every RedisPlacesCache instance. Closed at process exit."""
    from redis.asyncio import Redis

    client: Redis = Redis.from_url(url, decode_responses=True)
    return client


class RedisPlacesCache:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    @classmethod
    def from_url(cls, url: str) -> RedisPlacesCache:
        """Construct a cache backed by the shared per-URL Redis client."""
        return cls(_shared_redis_client(url))

    async def mget(self, provider_ids: list[str]) -> dict[str, PlaceObject]:
        if not provider_ids:
            return {}
        keys = [f"{_KEY_PREFIX}{pid}" for pid in provider_ids]
        try:
            values = await self._redis.mget(*keys)
        except Exception:
            logger.exception("places_v2_cache_mget_error")
            return {}

        result: dict[str, PlaceObject] = {}
        for pid, val in zip(provider_ids, values, strict=False):
            if val is not None:
                try:
                    result[pid] = PlaceObject.model_validate_json(val)
                except Exception:
                    logger.warning(
                        "places_v2_cache_decode_error",
                        extra={"provider_id": pid},
                    )
        return result

    async def mset(
        self, places: list[PlaceObject], ttl_seconds: int = PLACE_CACHE_TTL_SECONDS
    ) -> None:
        to_cache = [p for p in places if p.provider_id]
        if not to_cache:
            return
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                for place in to_cache:
                    key = f"{_KEY_PREFIX}{place.provider_id}"
                    pipe.set(key, place.model_dump_json(), ex=ttl_seconds)
                await pipe.execute()
        except Exception:
            logger.exception("places_v2_cache_mset_error")

    async def delete_many(self, provider_ids: list[str]) -> None:
        """Drop cache entries for the given provider_ids.

        Used by the wipe service to keep cache aligned with the DB wipe.
        Missing keys are a no-op in Redis — safe to call with stale ids.
        """
        if not provider_ids:
            return
        keys = [f"{_KEY_PREFIX}{pid}" for pid in provider_ids]
        try:
            await self._redis.delete(*keys)
        except Exception:
            logger.exception("places_v2_cache_delete_error")
