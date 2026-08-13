"""PlacesSearchService — DB → cache overlay → provider fallback.

Reads, plus the enrichment writes a read discovers. All writes are delegated
to PlaceUpsertService, which owns the merge policy and event emission — the
cold path persisting what the provider returned, and `_adopt_icon_hint`
letting an icon-less warm row learn the caller's icon. This service touches
the cache directly because cache stores the live half (PlaceObject) which is
shaped differently from the persisted PlaceCore.

`find` returns search results by query; `get_by_ids` returns enriched places
by namespaced provider_id. Stale DB rows (location wiped by the 30-day TTL
cron) are detected inline in `find` and routed through `get_by_ids` so the
provider repopulates both DB and cache in one pass.

Provider-agnostic: collaborates with PlacesClientProtocol; the concrete
implementation (Google, Foursquare, ...) is injected.
"""

from __future__ import annotations

import logging

from ._place_utils import overlay_with_cache, stamp_catalog_identity
from .models import PlaceCore, PlaceObject, PlaceQuery
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
        db_hits = await self._adopt_icon_hint(db_hits, query.icon_hint)

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

        Details responses intentionally carry no name (Essentials field mask,
        ADR-118) — the catalog row is the name authority (the merge keeps it
        sticky anyway), so nameless fetches are backfilled from the DB before
        persist/cache. A nameless fetch with no catalog name is dropped:
        a nameless place must never be persisted or cached.
        """
        if not provider_ids:
            return {}

        cached = await self._cache.mget(provider_ids)
        missing = [pid for pid in provider_ids if pid not in cached]
        if not missing:
            return cached

        fetched = await self._client.get_by_ids(missing)
        named = await self._backfill_names(fetched)
        stamped = await self._persist_external(named)

        fetched_map = {p.provider_id: p for p in stamped if p.provider_id}
        return {**cached, **fetched_map}

    async def get_cores_by_ids(self, place_core_ids: list[str]) -> dict[str, PlaceCore]:
        """Resolve persisted catalog rows by internal ``places.id``.

        DB-only: no cache overlay, no provider fallback, no upsert. This is
        the analytical/historical read path (ADR-077) — e.g. taste-profile
        regeneration aggregating already-saved places. It must not incur
        provider cost or mutate the catalog, which is exactly why it does
        not share the discovery path of ``find`` / ``get_by_ids``.

        Ids the catalog can't resolve are simply absent from the result.
        """
        if not place_core_ids:
            return {}
        cores = await self._repo.get_by_ids(place_core_ids)
        return {c.id: c for c in cores if c.id}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _backfill_names(self, fetched: list[PlaceObject]) -> list[PlaceObject]:
        """Fill empty place_names from the catalog; drop unresolvable ones.

        Only Place Details fetches arrive nameless (their mask omits
        displayName — ADR-118), and details is a refresh path: every id it
        is called with originated from a persisted row, so the catalog
        lookup is expected to hit. Named objects pass through untouched
        without a DB roundtrip.
        """
        nameless = [p for p in fetched if not p.place_name and p.provider_id]
        if not nameless:
            return fetched

        rows = await self._repo.get_by_provider_ids(
            [p.provider_id for p in nameless if p.provider_id]
        )
        result: list[PlaceObject] = []
        for obj in fetched:
            if obj.place_name:
                result.append(obj)
                continue
            row = rows.get(obj.provider_id) if obj.provider_id else None
            if row is None or not row.place_name:
                logger.warning("details_name_backfill_missing_row %s", obj.provider_id)
                continue
            result.append(obj.model_copy(update={"place_name": row.place_name}))
        return result

    async def _adopt_icon_hint(
        self, hits: list[PlaceCore], icon_hint: str | None
    ) -> list[PlaceCore]:
        """Let a warm row learn a caller-known icon it doesn't have (ADR-146).

        The cold path stamps `icon_hint` onto provider-fresh results so it
        rides the row's one normal upsert. A row that already existed never
        got that chance: the hint was stamped on the response copy for
        display and the row stayed NULL, so every later reader that lacks a
        hint of its own — `find_known`, the library — drew it blank forever.

        This is the one write on an otherwise read-only service, and it is
        deliberately narrow: only rows with no icon at all, only when the
        caller supplied one, once per row for its lifetime (the merge is
        fill-only, so a real icon is never overwritten). Embedding is
        skipped by hash, so the cost is a merge and an upsert.

        Failures are swallowed. This is an enrichment on a read path; a
        write problem must not cost the caller their search results.
        """
        if not icon_hint:
            return hits
        blank = [h for h in hits if not h.icon and h.provider_id]
        if not blank:
            return hits
        try:
            persisted = await self._upsert.upsert_and_embed(
                [h.model_copy(update={"icon": icon_hint}) for h in blank]
            )
        except Exception:
            logger.warning("icon_hint adoption failed", exc_info=True)
            return hits
        by_provider = {c.provider_id: c for c in persisted if c.provider_id}
        return [by_provider.get(h.provider_id or "", h) for h in hits]

    async def _external_fallback(
        self, query: PlaceQuery, limit: int
    ) -> list[PlaceObject]:
        """Cold path: client.search → upsert (via service) → cache → return.

        `query.icon_hint` is stamped onto provider-fresh results here —
        before `_persist_external` — so a caller-known icon rides the one
        normal upsert instead of needing a second write later. Provider
        results never carry an icon of their own today; the guard keeps
        a future provider-sourced icon authoritative anyway.
        """
        results = await self._client.search(query, limit)
        if query.icon_hint:
            results = [
                r if r.icon else r.model_copy(update={"icon": query.icon_hint})
                for r in results
            ]
        return await self._persist_external(results)

    async def _persist_external(self, places: list[PlaceObject]) -> list[PlaceObject]:
        """Persist a batch of provider-fetched places and return them with ids.

        Shared by the by-query cold path (``_external_fallback``) and the
        by-id cold path (``get_by_ids``). No-op on empty input so callers
        can stay branchless.

        The upsert mints/returns each row's catalog ``id``; those ids are
        stamped back onto the returned objects (keyed by ``provider_id``)
        before they are cached and handed to the caller. Otherwise a
        freshly-discovered place escapes with ``id=None`` and downstream
        save/signal — which key strictly on ``places.id`` — cannot attribute
        it. Caching the stamped objects keeps the cache warm with ids too.
        """
        if not places:
            return places
        persisted = await self._upsert.upsert_and_embed([p.to_core() for p in places])
        cores_by_provider = {c.provider_id: c for c in persisted if c.provider_id}
        stamped = stamp_catalog_identity(places, cores_by_provider)
        await self._cache.mset(stamped)
        return stamped
