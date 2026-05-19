"""PlaceWipeService — strip Google-derived location data past the ToS retention window.

Single-job service: compute a cutoff, wipe the DB, drop the same keys from
cache, log the result. Redis TTL would expire the cache eventually on its
own (same 30-day window), but explicit deletion keeps the two layers in
lockstep so a stale cache entry can't outlive its DB row.

No scheduler / cron / trigger here — wire it from a job runner if/when one
exists.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from .protocols import PlacesCacheProtocol, PlacesRepoProtocol

logger = logging.getLogger(__name__)

# Google ToS: cached/persisted Place data must not be retained beyond 30 days.
DEFAULT_RETENTION_DAYS: int = 30


class PlaceWipeService:
    def __init__(
        self,
        repo: PlacesRepoProtocol,
        cache: PlacesCacheProtocol,
    ) -> None:
        self._repo = repo
        self._cache = cache

    async def wipe_stale_locations(
        self, retention_days: int = DEFAULT_RETENTION_DAYS
    ) -> int:
        """Wipe location on rows last refreshed more than `retention_days` ago.

        Idempotent: subsequent runs only touch rows that have aged into the
        window since the last call. Returns the number of rows wiped.
        """
        cutoff = datetime.now(UTC) - timedelta(days=retention_days)
        wiped = await self._repo.wipe_stale_locations(cutoff)
        provider_ids = [c.provider_id for c in wiped if c.provider_id]
        if provider_ids:
            await self._cache.delete_many(provider_ids)
        logger.info(
            "place_locations_wiped",
            extra={
                "count": len(wiped),
                "cache_evicted": len(provider_ids),
                "cutoff": cutoff.isoformat(),
                "retention_days": retention_days,
            },
        )
        return len(wiped)
