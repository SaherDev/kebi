"""PlacesCache — Redis-backed Tier 2 (geo) + Tier 3 (enrichment) cache.

One class owns both tiers so the Redis client reference and the TTL stay in
sync. Both tiers use `config.places.cache_ttl_days * 86400` as the TTL; keys
live under `places:geo:{provider_id}` and `places:enrichment:{provider_id}`.

Read errors propagate to the caller (`PlacesService.enrich_batch`) which
treats them as "all miss" per FR-026a. Write errors are logged and swallowed
in place per FR-026b — the call returns successfully with whatever data was
in memory, and the cache catches up on the next successful write.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from redis.exceptions import RedisError

from kebi.core.config import get_config
from kebi.core.places.models import GeoData, PlaceEnrichment

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)


class PlacesCache:
    """Two-tier cache for Google Places geo + enrichment data (ADR-054, feature 019).

    Also carries a small reverse-geocode cache (`places:geocode:{cell_key}`)
    used by `PlacesService.resolve_location_label`. Same TTL as the other
    tiers — the underlying value (city + country) is essentially immutable,
    30 days is the project convention.
    """

    GEO_PREFIX = "places:geo:"
    ENRICHMENT_PREFIX = "places:enrichment:"
    GEOCODE_PREFIX = "places:geocode:"

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    # ------------------------------------------------------------------
    # TTL — single source of truth for both tiers. Computed lazily so
    # pytest overrides to config are picked up per-call.
    # ------------------------------------------------------------------
    @staticmethod
    def _ttl_seconds() -> int:
        return get_config().places.cache_ttl_days * 86400

    # ------------------------------------------------------------------
    # Tier 2 — geo
    # ------------------------------------------------------------------
    async def get_geo_batch(self, provider_ids: list[str]) -> dict[str, GeoData | None]:
        if not provider_ids:
            return {}
        keys = [f"{self.GEO_PREFIX}{pid}" for pid in provider_ids]
        raw: list[Any] = await self._redis.mget(keys)
        result: dict[str, GeoData | None] = {}
        for pid, value in zip(provider_ids, raw, strict=False):
            if value is None:
                result[pid] = None
                continue
            try:
                result[pid] = GeoData.model_validate_json(value)
            except ValueError as exc:
                # Diagnostic-only: the raw_value_prefix is captured so the
                # next repro of the `enriched: false` intermittent bug
                # shows us exactly what payload broke deserialization —
                # schema drift, partial write, encoding issue, etc. No
                # behavioral change; result[pid] still falls back to None.
                logger.warning(
                    "places.cache.deserialize_failed",
                    extra={
                        "tier": "geo",
                        "provider_id": pid,
                        "error": str(exc),
                        "raw_value_prefix": value[:100] if value else None,
                    },
                )
                result[pid] = None
        return result

    async def set_geo_batch(self, items: dict[str, GeoData]) -> None:
        if not items:
            return
        ttl = self._ttl_seconds()
        try:
            pipe = self._redis.pipeline(transaction=False)
            for pid, geo in items.items():
                pipe.set(
                    f"{self.GEO_PREFIX}{pid}",
                    geo.model_dump_json(),
                    ex=ttl,
                )
            await pipe.execute()
        except (TimeoutError, RedisError, ConnectionError) as exc:
            logger.warning(
                "places.cache.write_failed",
                extra={
                    "tier": "geo",
                    "key_count": len(items),
                    "error": str(exc),
                },
            )

    # ------------------------------------------------------------------
    # Tier 3 — enrichment
    # ------------------------------------------------------------------
    async def get_enrichment_batch(
        self, provider_ids: list[str]
    ) -> dict[str, PlaceEnrichment | None]:
        if not provider_ids:
            return {}
        keys = [f"{self.ENRICHMENT_PREFIX}{pid}" for pid in provider_ids]
        raw: list[Any] = await self._redis.mget(keys)
        result: dict[str, PlaceEnrichment | None] = {}
        for pid, value in zip(provider_ids, raw, strict=False):
            if value is None:
                result[pid] = None
                continue
            try:
                result[pid] = PlaceEnrichment.model_validate_json(value)
            except ValueError as exc:
                # Diagnostic-only: see the geo-tier comment above for
                # why raw_value_prefix is captured here.
                logger.warning(
                    "places.cache.deserialize_failed",
                    extra={
                        "tier": "enrichment",
                        "provider_id": pid,
                        "error": str(exc),
                        "raw_value_prefix": value[:100] if value else None,
                    },
                )
                result[pid] = None
        return result

    async def set_enrichment_batch(self, items: dict[str, PlaceEnrichment]) -> None:
        if not items:
            return

        # Programmer-error guard: HoursDict with any day key MUST carry a timezone
        # (data-model.md § 1.3). Catch mis-built payloads at the write boundary.
        _DAY_KEYS = frozenset(
            (
                "sunday",
                "monday",
                "tuesday",
                "wednesday",
                "thursday",
                "friday",
                "saturday",
            )
        )
        for pid, enr in items.items():
            hours = enr.hours
            if hours is None:
                continue
            has_day_key = any(k in hours for k in _DAY_KEYS)
            if has_day_key and not hours.get("timezone"):
                raise ValueError(
                    f"PlacesCache.set_enrichment_batch: provider_id={pid!r} "
                    "has HoursDict with day keys but no timezone — this is a "
                    "programmer error (see data-model.md § 1.3)."
                )

        ttl = self._ttl_seconds()
        try:
            pipe = self._redis.pipeline(transaction=False)
            for pid, enr in items.items():
                pipe.set(
                    f"{self.ENRICHMENT_PREFIX}{pid}",
                    enr.model_dump_json(),
                    ex=ttl,
                )
            await pipe.execute()
        except (TimeoutError, RedisError, ConnectionError) as exc:
            logger.warning(
                "places.cache.write_failed",
                extra={
                    "tier": "enrichment",
                    "key_count": len(items),
                    "error": str(exc),
                },
            )

    # ------------------------------------------------------------------
    # Reverse-geocode — cell_key (rounded lat/lng) → "<city>, <country>"
    # ------------------------------------------------------------------
    async def get_location_label(self, cell_key: str) -> str | None:
        try:
            raw = await self._redis.get(f"{self.GEOCODE_PREFIX}{cell_key}")
        except (TimeoutError, RedisError, ConnectionError) as exc:
            logger.warning(
                "places.cache.read_failed",
                extra={"tier": "geocode", "cell_key": cell_key, "error": str(exc)},
            )
            return None
        if raw is None:
            return None
        return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)

    async def set_location_label(self, cell_key: str, label: str) -> None:
        try:
            await self._redis.set(
                f"{self.GEOCODE_PREFIX}{cell_key}",
                label,
                ex=self._ttl_seconds(),
            )
        except (TimeoutError, RedisError, ConnectionError) as exc:
            logger.warning(
                "places.cache.write_failed",
                extra={"tier": "geocode", "cell_key": cell_key, "error": str(exc)},
            )
