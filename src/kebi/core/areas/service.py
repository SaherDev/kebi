"""AreaService — the read-through resolver over the area entity store.

One notion of "an area" for every subsystem (location-kinds Step 2):
store first, geocode on miss, persist what verifies. Generalizes the
ADR-126 recipe — a lookup is accepted only when it round-trips (the
returned feature slug-matches the asked-for name, inside the expected
country) and refused otherwise; a refused name is never substituted
with a nearby or similar entity.

Geometry compliance: coordinates are provider content and expire after
30 days; a store hit with stale geometry re-geocodes through the row's
stored place ID (which is storable indefinitely) before returning.
Best-effort — a failed refresh serves the stored geometry and retries
on the next read.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from kebi.core.areas.models import AreaContext, AreaEntity
from kebi.core.knowledge.schemas import (
    _slugify,
    build_geo_key,
    slugs_match,
)

if TYPE_CHECKING:
    from kebi.db.repositories.area_entity_repository import AreaEntityRepository
    from kebi.providers.geocoding import GeocodeResult, GeocoderProtocol

logger = logging.getLogger(__name__)

# Provider ToS: geocoded coordinates may be cached at most 30 days.
_GEOMETRY_MAX_AGE = timedelta(days=30)

# Google result types accepted as a "city" entity. Admin levels are
# deliberately included so provinces resolve (Hà Giang is an
# administrative_area_level_1) — ADR-124 already classifies provinces,
# islands, and towns at city scope.
_CITY_LEVEL_TYPES = frozenset(
    {
        "locality",
        "postal_town",
        "administrative_area_level_1",
        "administrative_area_level_2",
    }
)


class AreaService:
    """Store-first area resolution, verified-or-refuse (ADR-126)."""

    def __init__(
        self,
        repo: AreaEntityRepository,
        geocoder: GeocoderProtocol,
    ) -> None:
        self._repo = repo
        self._geocoder = geocoder
        # Per-instance memo (one instance per request/harvest): repeated
        # names skip even the store read. The DB is the durable memo.
        self._memo: dict[tuple[str, str], AreaEntity | None] = {}

    async def get(self, entity_key: str) -> AreaEntity | None:
        """Store-only read (+ compliance geometry refresh when stale)."""
        entity = await self._repo.get(entity_key)
        if entity is None:
            return None
        return await self._fresh(entity)

    async def resolve_country(self, name: str) -> AreaEntity | None:
        """Resolve a country name (or literal alpha-2 code) to its entity.

        Verified by feature type: the geocoder match must *be* a country,
        not a street or venue named after one.
        """
        memo_key = ("country", _slugify(name))
        if memo_key in self._memo:
            return self._memo[memo_key]
        entity = await self._resolve_country_uncached(name)
        self._memo[memo_key] = entity
        return entity

    async def resolve_city(self, name: str, country_code: str) -> AreaEntity | None:
        """Resolve a city/province name within a country, round-trip verified.

        Store hit on the canonical key first, then a learned-alias hit,
        then one geocode constrained to the country — accepted only when
        the returned feature is city-level, slug-matches the asked name,
        and sits in the asked country. Refusal returns `None`; the caller
        clarifies or drops, never substitutes.
        """
        cc = country_code.strip().lower()
        memo_key = ("city", f"{cc}:{_slugify(name)}")
        if memo_key in self._memo:
            return self._memo[memo_key]
        entity = await self._resolve_city_uncached(name, cc)
        self._memo[memo_key] = entity
        return entity

    async def resolve_noted_name(
        self, name: str, context: AreaContext, *, probe_name: bool = True
    ) -> AreaEntity | None:
        """Resolve a noted non-venue name to the entity its interest belongs to.

        The subject-vs-container rule: the name itself is tried as an area
        first ("Hoi An", "Mui Ne" — administrative rejections resolve to
        themselves); a name that refuses (routes — "Ha Giang Loop" never
        slug-matches a locality) collapses to its *containing* area from
        the share's context (city, else country), per the roadmap's
        external-routes-are-untrusted rule. Callers that already know the
        name is a route (`non_venue_route` detections) pass
        `probe_name=False` to skip the doomed name-as-area geocode.
        """
        codes = await self._context_country_codes(context)
        if probe_name:
            for cc in codes:
                entity = await self.resolve_city(name, cc)
                if entity is not None:
                    return entity
        if context.city:
            for cc in codes:
                entity = await self.resolve_city(context.city, cc)
                if entity is not None:
                    return entity
        if codes:
            return await self.resolve_country(codes[0])
        return None

    # ---- resolution internals -------------------------------------------

    async def _resolve_country_uncached(self, name: str) -> AreaEntity | None:
        asked = name.strip()
        cc = asked.lower() if len(asked) == 2 and asked.isalpha() else None
        if cc is not None:
            stored = await self._repo.get(build_geo_key(cc))
            if stored is not None:
                return await self._fresh(stored)
        result = await self._search(query=asked, region_code=cc)
        if result is None or result.place_type != "country" or not result.country_code:
            return None
        code = result.country_code
        stored = await self._repo.get(build_geo_key(code))
        if stored is not None and cc is None:
            # Asked by name, matched an already-stored country.
            return await self._fresh(stored)
        return await self._persist(
            entity_key=build_geo_key(code),
            entity_type="country",
            display_name=result.country or result.name or asked,
            country_code=code,
            asked_name=asked,
            result=result,
            parent_key=None,
        )

    async def _resolve_city_uncached(self, name: str, cc: str) -> AreaEntity | None:
        try:
            key = build_geo_key(cc, name)
        except ValueError:
            return None
        stored = await self._repo.get(key)
        if stored is not None:
            return await self._fresh(stored)
        alias_hit = await self._repo.find_by_alias(cc, _slugify(name))
        if alias_hit is not None:
            return await self._fresh(alias_hit)
        result = await self._search(query=name, region_code=cc)
        if result is None or result.country_code != cc:
            return None
        if result.place_type not in _CITY_LEVEL_TYPES:
            return None
        # Round-trip: the matched feature must BE the asked-for name —
        # its own name or the component that forms the key (ADR-126).
        if result.city and slugs_match(name, result.city):
            canonical = result.city
        elif result.name and slugs_match(name, result.name):
            canonical = result.name
        else:
            return None
        return await self._persist(
            entity_key=build_geo_key(cc, canonical),
            entity_type="city",
            display_name=canonical,
            country_code=cc,
            asked_name=name,
            result=result,
            parent_key=build_geo_key(cc),
        )

    async def _persist(
        self,
        *,
        entity_key: str,
        entity_type: str,
        display_name: str,
        country_code: str,
        asked_name: str,
        result: GeocodeResult,
        parent_key: str | None,
    ) -> AreaEntity:
        aliases = []
        asked_slug = _slugify(asked_name)
        if asked_slug and asked_slug != _slugify(display_name):
            aliases.append(asked_slug)
        entity = AreaEntity(
            entity_key=entity_key,
            entity_type=entity_type,
            name=display_name,
            aliases=aliases,
            country_code=country_code,
            lat=result.lat,
            lng=result.lng,
            bbox=result.bbox,
            place_type=result.place_type,
            parent_key=parent_key,
            provider_id=result.provider_id,
            geo_refreshed_at=datetime.now(UTC),
        )
        return await self._repo.upsert(entity)

    # ---- helpers ---------------------------------------------------------

    async def _context_country_codes(self, context: AreaContext) -> list[str]:
        codes: list[str] = []
        if context.country_code:
            codes.append(context.country_code.strip().lower())
        elif context.country:
            country = await self.resolve_country(context.country)
            if country is not None:
                codes.append(country.country_code)
        return codes

    async def _fresh(self, entity: AreaEntity) -> AreaEntity:
        """Compliance refresh: re-geocode stale geometry through the stored
        place ID. Best-effort — the stored entity is served on failure."""
        age_ok = (
            entity.geo_refreshed_at is not None
            and datetime.now(UTC) - entity.geo_refreshed_at < _GEOMETRY_MAX_AGE
        )
        if age_ok or not entity.provider_id:
            return entity
        try:
            result = await self._geocoder.geocode_place_id(entity.provider_id)
        except Exception as exc:
            logger.warning(
                "area geometry refresh failed for %s: %s", entity.entity_key, exc
            )
            return entity
        if result is None:
            return entity
        now = datetime.now(UTC)
        await self._repo.update_geometry(
            entity.entity_key,
            lat=result.lat,
            lng=result.lng,
            bbox=result.bbox,
            refreshed_at=now,
        )
        return entity.model_copy(
            update={
                "lat": result.lat,
                "lng": result.lng,
                "bbox": result.bbox,
                "geo_refreshed_at": now,
            }
        )

    async def _search(
        self, *, query: str, region_code: str | None
    ) -> GeocodeResult | None:
        try:
            return await self._geocoder.search_area(
                query=query, region_code=region_code
            )
        except Exception as exc:
            logger.warning("area geocode failed for %r: %s", query, exc)
            return None
