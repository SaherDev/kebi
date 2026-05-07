"""PlacesSearchService — DB → cache overlay → provider fallback.

Reads only. All writes are delegated to PlaceUpsertService, which owns the
merge policy and event emission. This service touches the cache directly
because cache stores the live half (PlaceObject) which is shaped differently
from the persisted PlaceCore.

`find` returns search results by query; `get_by_ids` returns enriched places
by namespaced provider_id. Stale DB rows (location wiped by the 30-day TTL
cron) are detected inline in `find` and routed through `get_by_ids` so the
provider repopulates both DB and cache in one pass.

Provider-agnostic: collaborates with PlacesClientProtocol; the concrete
implementation (Google, Foursquare, ...) is injected.
"""

from __future__ import annotations

import logging

from ._place_utils import overlay_with_cache
from .models import PlaceObject, PlaceQuery
from .protocols import (
    PlacesCacheProtocol,
    PlacesClientProtocol,
    PlacesRepoProtocol,
    PlaceUpsertServiceProtocol,
)

logger = logging.getLogger(__name__)


class PlacesSearchService:
    def __init__(
        self,
        repo: PlacesRepoProtocol,
        cache: PlacesCacheProtocol,
        client: PlacesClientProtocol,
        upsert_service: PlaceUpsertServiceProtocol,
    ) -> None:
        self._repo = repo
        self._cache = cache
        self._client = client
        self._upsert = upsert_service

    async def find(self, query: PlaceQuery, limit: int = 20) -> list[PlaceObject]:
        """DB → enrich (cache + provider fallback) → external query fallback
        if no DB hits."""
        db_hits = await self._repo.find(query, limit)
        if not db_hits:
            return await self._external_fallback(query, limit)

        # get_by_ids hits cache first; only misses (incl. TTL-wiped stale
        # rows) go to the provider, with upsert + mset on the way back.
        provider_ids = [c.provider_id for c in db_hits if c.provider_id]
        enriched = await self.get_by_ids(provider_ids)

        return overlay_with_cache(db_hits, enriched)

    async def get_by_ids(self, provider_ids: list[str]) -> dict[str, PlaceObject]:
        """Resolve places by provider_id with cache → external fallback.

        Cache hits are returned directly. Misses are fetched from the provider
        via ``client.get_by_ids`` (Place Details), then upserted to the DB and
        written to cache so subsequent calls stay warm. Ids the provider can't
        resolve are simply absent from the result dict.
        """
        if not provider_ids:
            return {}

        cached = await self._cache.mget(provider_ids)
        missing = [pid for pid in provider_ids if pid not in cached]
        if not missing:
            return cached

        fetched = await self._client.get_by_ids(missing)
        await self._persist_external(fetched)

        fetched_map = {p.provider_id: p for p in fetched if p.provider_id}
        return {**cached, **fetched_map}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _external_fallback(
        self, query: PlaceQuery, limit: int
    ) -> list[PlaceObject]:
        """Cold path: client.search → upsert (via service) → cache → return."""
        results = await self._client.search(query, limit)
        await self._persist_external(results)
        return results

    async def _persist_external(self, places: list[PlaceObject]) -> None:
        """Persist a batch of provider-fetched places: upsert DB + write cache.

        Shared by the by-query cold path (``_external_fallback``) and the
        by-id cold path (``get_by_ids``). No-op on empty input so callers
        can stay branchless.
        """
        if not places:
            return
        await self._upsert.upsert_many([p.to_core() for p in places])
        await self._cache.mset(places)
