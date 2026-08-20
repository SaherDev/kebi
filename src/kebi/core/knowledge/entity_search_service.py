"""Entity typeahead behind the curation anchor chip (ADR-121 extension).

One query, one typed result set: catalog places through the existing
unscoped hybrid search, areas from the `areas` table by name — and, only
when the known corpus has no area hit, a verified-or-refuse geocode so a
never-opened area can still be anchored. No LLM anywhere; every id returned
is an anchor payload the curate endpoint accepts verbatim.

The registry fallback deliberately inherits the knowledge layer's refusal
posture (ADR-126): every name resolves through the geo registry's verified
lookup or not at all. Its verdicts — including misses — are Redis-cached on
the normalized query, so typeahead traffic can't turn into a stream of
mint-time geocoder calls.
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, ConfigDict

from kebi.core.geo.models import GeoArea
from kebi.core.geo.protocols import GeoRegistryProtocol
from kebi.core.knowledge.schemas import _slugify
from kebi.core.places.hybrid_search_service import HybridSearchService
from kebi.core.places.models import PlaceCore
from kebi.db.repositories.area_repository import AreaRepository
from kebi.providers.cache import CacheBackend

logger = logging.getLogger(__name__)

# v3: keys became geo-registry id-paths — cached v2 verdicts carry slug
# keys and must not shadow the new grammar for their TTL.
_CACHE_PREFIX = "entsearch:v3:"
_CACHE_MISS = '{"miss": true}'


class _ResolverCache(BaseModel):
    """Shape of one cached resolver verdict (hit form)."""

    model_config = ConfigDict(frozen=True)

    geo_key: str
    name: str
    level: str
    context: str | None = None


class AreaHit(BaseModel):
    """One area result, whatever corpus it came from. `level` is a display
    label (an areas row's own, or the resolver's structural verdict);
    `context` is the human ancestor line ("Bali, Indonesia"), composed here
    because key segments carry nothing readable."""

    model_config = ConfigDict(frozen=True)

    geo_key: str
    name: str
    level: str
    context: str | None = None


class EntitySearchResults(BaseModel):
    """Areas first, then places — the composition the chip renders."""

    model_config = ConfigDict(frozen=True)

    areas: list[AreaHit]
    places: list[PlaceCore]


def _city_hit(row: GeoArea | None) -> AreaHit | None:
    if row is None:
        return None
    return AreaHit(
        geo_key=row.geo_key,
        name=row.display_name,
        level="city",
        context=row.country_code.upper(),
    )


class EntitySearchService:
    """Deterministic lookup: DB corpus always, resolver only on area miss."""

    def __init__(
        self,
        area_repo: AreaRepository,
        hybrid_search: HybridSearchService,
        geo_registry: GeoRegistryProtocol,
        *,
        cache: CacheBackend | None = None,
        cache_ttl_seconds: int = 604800,
        area_limit: int = 3,
    ) -> None:
        self._area_repo = area_repo
        self._hybrid = hybrid_search
        self._registry = geo_registry
        self._cache = cache
        self._cache_ttl = cache_ttl_seconds
        self._area_limit = area_limit

    async def search(self, q: str, limit: int) -> EntitySearchResults:
        query = q.strip()
        if not query:
            return EntitySearchResults(areas=[], places=[])

        areas = [
            AreaHit(
                geo_key=p.geo_key,
                name=p.name,
                level=p.level,
                context=", ".join(reversed(p.breadcrumb)) or None,
            )
            for p in await self._area_repo.search_by_name(query, self._area_limit)
        ]
        if not areas:
            resolved = await self._resolve_new_area(query)
            if resolved is not None:
                areas = [resolved]

        place_budget = max(0, limit - len(areas))
        places: list[PlaceCore] = []
        if place_budget:
            hits = await self._hybrid.search(
                user_id=None, query=query, limit=place_budget
            )
            places = [h.place for h in hits]
        return EntitySearchResults(areas=areas, places=places)

    async def _resolve_new_area(self, query: str) -> AreaHit | None:
        """Verified geocode for an unseen area name, cached verdict-or-miss.

        "Name, Country" resolves as a city constrained to that country; a
        bare name resolves only as a country. Anything unverifiable is None
        — the chip simply has no area row to offer, never a guess.
        """
        cache_key = _CACHE_PREFIX + _slugify(query)
        cached = await self._cache_get(cache_key)
        if cached is not None:
            return cached if isinstance(cached, AreaHit) else None

        hit = await self._resolve_uncached(query)
        await self._cache_set(cache_key, hit)
        return hit

    async def _resolve_uncached(self, query: str) -> AreaHit | None:
        name, _, country = query.partition(",")
        name, country = name.strip(), country.strip()
        if country:
            country_row = await self._registry.resolve_country(country)
            if country_row is None:
                return None
            resolved = await self._registry.key_for_location(
                country_row.country_code, name, None, mint=True
            )
            return _city_hit(resolved.city if resolved else None)
        # Bare name: a country first ("Vietnam" must never resolve as some
        # city named Vietnam), then a verified unconstrained city lookup —
        # prominence-ranked, round-trip checked ("Tokyo" resolves).
        row = await self._registry.resolve_country(name)
        if row is not None:
            return AreaHit(geo_key=row.geo_key, name=row.display_name, level="country")
        return _city_hit(await self._registry.resolve_city_global(name))

    # ---- cache (degrade-don't-fail: any Redis error means "no cache") ----

    async def _cache_get(self, key: str) -> AreaHit | None | str:
        """Returns an AreaHit on a cached hit, the sentinel string on a
        cached miss, and None when the cache has nothing to say."""
        if self._cache is None:
            return None
        try:
            raw = await self._cache.get(key)
        except Exception as exc:
            logger.warning("entity search cache read failed: %s", exc)
            return None
        if raw is None:
            return None
        if raw == _CACHE_MISS:
            return raw
        try:
            data = _ResolverCache.model_validate(json.loads(raw))
        except ValueError:
            return None
        return AreaHit(
            geo_key=data.geo_key,
            name=data.name,
            level=data.level,
            context=data.context,
        )

    async def _cache_set(self, key: str, hit: AreaHit | None) -> None:
        if self._cache is None:
            return
        value = _CACHE_MISS if hit is None else hit.model_dump_json()
        try:
            await self._cache.set(key, value, self._cache_ttl)
        except Exception as exc:
            logger.warning("entity search cache write failed: %s", exc)
