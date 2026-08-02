"""CachedGeocoder — Redis-backed reverse-geocode cache.

Decorator over any `GeocoderProtocol`. Only `reverse` is cached: GPS
lookups repeat heavily (home-screen opens, agent turns from the same
spot), and rounding to ~110 m buckets collapses them onto few keys.
Forward lookups need no Redis cache — the `area_entities` store is the
durable forward cache (each unique area geocodes once ever), and the
place-id refresh path must always hit the provider by design.

Misses cache too (as a null marker, short TTL) so open ocean / empty
coords don't re-bill on every call. Fail-open: a Redis error falls
through to the wrapped geocoder — a flaky cache must never take
location resolution down. The TTL is capped at 30 days to honor the
provider's result-caching terms.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .protocol import GeocodeResult

if TYPE_CHECKING:
    from redis.asyncio import Redis

    from .protocol import GeocoderProtocol

logger = logging.getLogger(__name__)

_KEY_PREFIX = "geo:rev:"
_MISS_MARKER = "null"

# Provider ToS cap results caching at 30 days; misses re-check sooner.
MAX_TTL_SECONDS: int = 30 * 24 * 60 * 60
_MISS_TTL_SECONDS: int = 24 * 60 * 60


class CachedGeocoder:
    def __init__(
        self,
        inner: GeocoderProtocol,
        redis: Redis,
        *,
        ttl_seconds: int = MAX_TTL_SECONDS,
        precision: int = 3,
    ) -> None:
        self._inner = inner
        self._redis = redis
        self._ttl_seconds = min(ttl_seconds, MAX_TTL_SECONDS)
        self._precision = precision

    async def search_area(
        self, *, query: str, region_code: str | None = None
    ) -> GeocodeResult | None:
        return await self._inner.search_area(query=query, region_code=region_code)

    async def geocode_place_id(self, place_id: str) -> GeocodeResult | None:
        return await self._inner.geocode_place_id(place_id)

    async def reverse(self, *, lat: float, lng: float) -> GeocodeResult | None:
        key = self._key(lat, lng)
        try:
            raw = await self._redis.get(key)
        except Exception:
            logger.exception("geo_reverse_cache_get_error")
            return await self._inner.reverse(lat=lat, lng=lng)
        if raw is not None:
            text = raw.decode() if isinstance(raw, bytes) else raw
            if text == _MISS_MARKER:
                return None
            try:
                cached = GeocodeResult.model_validate_json(text)
            except Exception:
                logger.warning("geo_reverse_cache_decode_error")
            else:
                # The bucket's names are shared; the caller's exact GPS
                # stays canonical (reverse echoes its input coords).
                return cached.model_copy(update={"lat": lat, "lng": lng})

        result = await self._inner.reverse(lat=lat, lng=lng)
        try:
            if result is None:
                await self._redis.set(key, _MISS_MARKER, ex=_MISS_TTL_SECONDS)
            else:
                await self._redis.set(
                    key, result.model_dump_json(), ex=self._ttl_seconds
                )
        except Exception:
            logger.exception("geo_reverse_cache_set_error")
        return result

    def _key(self, lat: float, lng: float) -> str:
        p = self._precision
        return f"{_KEY_PREFIX}{round(lat, p)}:{round(lng, p)}"
