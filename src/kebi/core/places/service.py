"""PlacesService — the public data-layer entry point for every caller.

Implements create/create_batch/get/get_batch/get_by_external_id plus the
two enrich_batch modes: `geo_only=True` (recall — Tier 2 only, zero provider
calls) and `geo_only=False` (consult — both tiers, fetch on miss, bounded
by `config.places.max_enrichment_batch`).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC
from typing import TYPE_CHECKING, Any

from redis.exceptions import RedisError

from kebi.core.config import get_config
from kebi.core.places.models import (
    GeoData,
    PlaceCreate,
    PlaceEnrichment,
    PlaceObject,
    PlaceProvider,
)
from kebi.core.places.repository import PlacesRepository

if TYPE_CHECKING:
    from kebi.core.places.cache import PlacesCache
    from kebi.core.places.places_client import PlacesClient

logger = logging.getLogger(__name__)


class PlacesService:
    """Facade over PlacesRepository + PlacesCache + PlacesClient.

    Callers receive a fully-wired instance via `Depends(get_places_service)`.
    During Phase 3, `cache` and `client` are optional (the create/get paths
    don't need them); Phase 4/5 make them required for `enrich_batch`.
    """

    def __init__(
        self,
        repo: PlacesRepository,
        cache: PlacesCache | None = None,
        client: PlacesClient | None = None,
    ) -> None:
        self._repo = repo
        self._cache = cache
        self._client = client

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    async def create(self, data: PlaceCreate) -> PlaceObject:
        return await self._repo.create(data)

    async def create_batch(self, items: list[PlaceCreate]) -> list[PlaceObject]:
        return await self._repo.create_batch(items)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    async def get(self, place_id: str) -> PlaceObject | None:
        return await self._repo.get(place_id)

    async def get_batch(self, place_ids: list[str]) -> list[PlaceObject]:
        # Positional-alignment audit (feature 019, T071a):
        # `get_batch` silently drops IDs the DB can't find, so
        # `len(output) <= len(input)` and positional indices do NOT align.
        # Every call site in src/kebi was audited for `zip(...)`-style
        # parallel-array joins against the input list; none exist. If a new
        # caller needs to pair inputs with results, re-key the output by
        # `place_id` into a dict, or call `get(place_id)` per ID — do NOT
        # assume positional alignment.
        return await self._repo.get_batch(place_ids)

    async def get_by_external_id(
        self, provider: PlaceProvider, external_id: str
    ) -> PlaceObject | None:
        return await self._repo.get_by_external_id(provider, external_id)

    async def count_for_user(self, user_id: str) -> int:
        return await self._repo.count_for_user(user_id)

    # ------------------------------------------------------------------
    # Enrichment
    # ------------------------------------------------------------------
    async def enrich_batch(
        self,
        places: list[PlaceObject],
        geo_only: bool = False,
        priority_provider_ids: set[str] | None = None,
    ) -> list[PlaceObject]:
        """Attach Tier 2 (and optionally Tier 3) data to each input place.

        In `geo_only=True` (recall) mode, reads only the geo cache. Misses
        stay as Tier 2 None / geo_fresh=False. Zero provider calls.

        In `geo_only=False` (consult) mode, reads both caches, fetches the
        union of misses via a single `get_place_details` call per unique
        provider_id (bounded by `config.places.max_enrichment_batch`), and
        writes both cache tiers from the merged response.

        `priority_provider_ids` is an optional set of provider_ids the
        caller wants guaranteed enrichment for. When the total miss count
        exceeds the cap, priority ids are fetched first and the remaining
        budget goes to non-priority ids (both sub-lists alphabetically
        sorted for determinism). `ConsultService` uses this to ensure the
        user's saved places are never dropped in favor of discovered
        candidates with earlier Google Place IDs.

        Output preserves input order. Places with `provider_id=None` pass
        through unchanged.
        """
        if not places:
            return []

        unique_ids: list[str] = []
        seen: set[str] = set()
        for place in places:
            pid = place.provider_id
            if pid is None or pid in seen:
                continue
            seen.add(pid)
            unique_ids.append(pid)

        if not unique_ids:
            return [p.model_copy() for p in places]

        if self._cache is None:
            raise RuntimeError(
                "PlacesService.enrich_batch requires a PlacesCache; none injected."
            )

        geo_hits = await self._read_geo_cache(unique_ids)

        if geo_only:
            return self._merge_geo_only(places, geo_hits)

        enr_hits = await self._read_enrichment_cache(unique_ids)
        new_geo, new_enr = await self._fetch_and_split_misses(
            unique_ids, geo_hits, enr_hits, priority_provider_ids
        )

        # Best-effort writeback. Cache.set_*_batch swallow errors internally.
        if new_geo:
            await self._cache.set_geo_batch(new_geo)
        if new_enr:
            await self._cache.set_enrichment_batch(new_enr)

        combined_geo: dict[str, GeoData | None] = dict(geo_hits)
        combined_geo.update(new_geo)
        combined_enr: dict[str, PlaceEnrichment | None] = dict(enr_hits)
        combined_enr.update(new_enr)

        return self._merge_full(places, combined_geo, combined_enr)

    # ------------------------------------------------------------------
    # Location labels (reverse geocoding, cache-or-fetch)
    # ------------------------------------------------------------------
    async def resolve_location_label(
        self, lat: float, lng: float
    ) -> str | None:
        """Resolve precise coords to a city/country label ("Magdeburg, Germany").

        Keyed on a ~11 km grid cell (1 decimal place of lat/lng). Cache hit
        returns in ~5-15ms; miss triggers a single Google Geocoding call and
        populates the cache. Returns None on failure so callers can degrade
        gracefully (the agent just won't see a city hint — same as today).
        """
        if self._cache is None or self._client is None:
            return None
        cell_key = f"{round(lat, 1)},{round(lng, 1)}"
        cached = await self._cache.get_location_label(cell_key)
        if cached is not None:
            return cached
        label = await self._client.reverse_geocode(lat, lng)
        if label is not None:
            await self._cache.set_location_label(cell_key, label)
        return label

    # ------------------------------------------------------------------
    # Enrichment internals
    # ------------------------------------------------------------------
    async def _read_geo_cache(self, unique_ids: list[str]) -> dict[str, GeoData | None]:
        assert self._cache is not None
        try:
            return await self._cache.get_geo_batch(unique_ids)
        except (TimeoutError, RedisError, ConnectionError) as exc:
            logger.warning(
                "places.cache.read_failed",
                extra={
                    "tier": "geo",
                    "provider_id_count": len(unique_ids),
                    "error": str(exc),
                },
            )
            return {pid: None for pid in unique_ids}

    async def _read_enrichment_cache(
        self, unique_ids: list[str]
    ) -> dict[str, PlaceEnrichment | None]:
        assert self._cache is not None
        try:
            return await self._cache.get_enrichment_batch(unique_ids)
        except (TimeoutError, RedisError, ConnectionError) as exc:
            logger.warning(
                "places.cache.read_failed",
                extra={
                    "tier": "enrichment",
                    "provider_id_count": len(unique_ids),
                    "error": str(exc),
                },
            )
            return {pid: None for pid in unique_ids}

    async def _fetch_and_split_misses(
        self,
        unique_ids: list[str],
        geo_hits: dict[str, GeoData | None],
        enr_hits: dict[str, PlaceEnrichment | None],
        priority_provider_ids: set[str] | None,
    ) -> tuple[dict[str, GeoData], dict[str, PlaceEnrichment]]:
        if self._client is None:
            raise RuntimeError(
                "PlacesService.enrich_batch requires a PlacesClient in consult mode."
            )

        miss_set: set[str] = set()
        for pid in unique_ids:
            if geo_hits.get(pid) is None or enr_hits.get(pid) is None:
                miss_set.add(pid)

        if not miss_set:
            return {}, {}

        # Two-pass priority sort: priority ids first (alphabetically),
        # then non-priority ids (alphabetically). Cap applied after the
        # sort so priority ids are kept and non-priority tail is dropped.
        priority_set = priority_provider_ids or set()
        priority_misses = sorted(m for m in miss_set if m in priority_set)
        other_misses = sorted(m for m in miss_set if m not in priority_set)
        misses = priority_misses + other_misses

        cap = get_config().places.max_enrichment_batch
        if len(misses) > cap:
            dropped = len(misses) - cap
            dropped_priority = max(0, len(priority_misses) - cap)
            logger.warning(
                "places.enrichment.fetch_cap_exceeded",
                extra={
                    "requested": len(misses),
                    "cap": cap,
                    "dropped": dropped,
                    "dropped_priority": dropped_priority,
                },
            )
            misses = misses[:cap]

        responses = await asyncio.gather(
            *[self._client.get_place_details(_strip_namespace(pid)) for pid in misses],
            return_exceptions=True,
        )

        new_geo: dict[str, GeoData] = {}
        new_enr: dict[str, PlaceEnrichment] = {}
        for pid, response in zip(misses, responses, strict=False):
            if isinstance(response, BaseException):
                logger.warning(
                    "places.enrichment.fetch_failed",
                    extra={"provider_id": pid, "error": str(response)},
                )
                continue
            if response is None:
                logger.warning(
                    "places.enrichment.fetch_failed",
                    extra={"provider_id": pid, "error": "provider returned None"},
                )
                continue
            geo, enr = _map_provider_response(response)
            if geo is not None:
                new_geo[pid] = geo
            if enr is not None:
                new_enr[pid] = enr
        return new_geo, new_enr

    @staticmethod
    def _merge_geo_only(
        places: list[PlaceObject],
        geo_hits: dict[str, GeoData | None],
    ) -> list[PlaceObject]:
        out: list[PlaceObject] = []
        for place in places:
            pid = place.provider_id
            if pid is None:
                out.append(place.model_copy())
                continue
            geo = geo_hits.get(pid)
            if geo is None:
                out.append(place.model_copy())
                continue
            out.append(
                place.model_copy(
                    update={
                        "lat": geo.lat,
                        "lng": geo.lng,
                        "address": geo.address,
                        "geo_fresh": True,
                    }
                )
            )
        return out

    @staticmethod
    def _merge_full(
        places: list[PlaceObject],
        geo_by_pid: dict[str, GeoData | None],
        enr_by_pid: dict[str, PlaceEnrichment | None],
    ) -> list[PlaceObject]:
        out: list[PlaceObject] = []
        for place in places:
            pid = place.provider_id
            if pid is None:
                out.append(place.model_copy())
                continue
            update: dict[str, Any] = {}
            geo = geo_by_pid.get(pid)
            if geo is not None:
                update["lat"] = geo.lat
                update["lng"] = geo.lng
                update["address"] = geo.address
                update["geo_fresh"] = True
            enr = enr_by_pid.get(pid)
            if enr is not None:
                update["hours"] = enr.hours
                update["rating"] = enr.rating
                update["phone"] = enr.phone
                update["photo_url"] = enr.photo_url
                update["popularity"] = enr.popularity
                update["enriched"] = True
            out.append(
                place.model_copy(update=update) if update else place.model_copy()
            )
        return out


# ---------------------------------------------------------------------------
# Namespace parsing — the ONLY parse site in the codebase.
# PlacesRepository._build_provider_id is the ONLY construction site.
# ---------------------------------------------------------------------------


def _strip_namespace(provider_id: str) -> str:
    """Strip the `{provider}:` prefix and return the raw external_id."""
    return provider_id.split(":", 1)[1]


def _map_provider_response(
    response: dict[str, Any],
) -> tuple[GeoData | None, PlaceEnrichment | None]:
    """Split one `get_place_details` dict into (GeoData, PlaceEnrichment).

    Never issues a second API call. Both halves may be present, only one,
    or neither — the caller writes whichever half is non-None.
    """
    from datetime import datetime

    now = datetime.now(UTC)

    lat = response.get("lat")
    lng = response.get("lng")
    address = response.get("address")

    geo: GeoData | None = None
    if lat is not None and lng is not None and address:
        geo = GeoData(
            lat=float(lat),
            lng=float(lng),
            address=str(address),
            cached_at=now,
        )

    enr = PlaceEnrichment(
        hours=response.get("hours"),
        rating=response.get("rating"),
        phone=response.get("phone"),
        photo_url=response.get("photo_url"),
        popularity=response.get("popularity"),
        fetched_at=now,
    )
    return geo, enr
